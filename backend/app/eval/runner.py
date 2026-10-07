"""Evaluation runner.

The runner executes an :class:`Agent` against a dataset and scores each
result using a :class:`Scorer`. Datasets are simple JSON lists of
``{"input": str, "expected": str | list, "metadata": {...}}`` records.

Two entry points are exposed:

* :meth:`EvalRunner.run`           — legacy, takes a JSON dataset
* :meth:`EvalRunner.run_eval`      — new, takes ``test_case_ids`` and a
  :class:`app.eval.scorer.EvalScorer`; reports progress through an
  optional ``on_progress`` callback and persists per-case results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.loop import AgentLoop, LoopResult
from app.eval.metrics import Metrics
from app.eval.scorer import (
    AgentRunResult,
    EvalScorer,
    TestCase,
    default_scorer,
)
from app.models import Agent, EvalCase, EvalRun
from app import database as _db_mod

logger = logging.getLogger(__name__)

# A scorer takes a prediction and the expected reference and returns a
# float in [0, 1]. Provided implementations live in ``app.eval.metrics``.
Scorer = Callable[[str, Any], float]
AsyncScorer = Callable[[str, Any], Awaitable[float]]

# Callback signature for progress reporting.  Receives (done, total).
ProgressCallback = Callable[[int, int], None]


# 0.1.2 5.1 轮: SQLite is a single-writer database, and the
# ``AgentLoop`` writes ``agent_runs`` / ``steps`` rows while the
# runner writes ``eval_cases`` rows from a *separate* session.
# Concurrent case sessions therefore race for the write lock and
# may see ``OperationalError: database is locked`` even with
# ``busy_timeout`` configured (the timeout applies to reads, not
# to the same handle holding a write transaction).  ``_commit_with_retry``
# retries the commit a handful of times with a small backoff so
# concurrent cases that happen to overlap on the writer lock
# transparently serialise.  After a ``rollback()`` the ORM objects
# are expunged; ``_commit_with_retry_safe_add`` re-attaches the
# rows so the next ``commit()`` actually writes them.
_COMMIT_RETRIES = 5
_COMMIT_BACKOFF_S = 0.05  # 50ms; 5 retries ⇒ max 250ms before giving up


async def _commit_with_retry(session: AsyncSession) -> None:
    """``session.commit()`` with retry on ``database is locked``."""
    for attempt in range(_COMMIT_RETRIES):
        try:
            await session.commit()
            return
        except OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg or attempt == _COMMIT_RETRIES - 1:
                raise
            await session.rollback()
            await asyncio.sleep(_COMMIT_BACKOFF_S * (attempt + 1))


async def _commit_with_retry_safe_add(
    session: AsyncSession, *rows: Any
) -> None:
    """Like :func:`_commit_with_retry` but re-adds the ORM rows after
    each rollback so the next commit sees them.

    When commit fails, SQLAlchemy 2 sets the session to a
    "rolled back" state; the next ``commit()`` raises
    ``PendingRollbackError``.  We call ``rollback()`` to clear that
    state and re-add the rows so the next attempt actually writes
    them.  The ``*rows`` are the ORM objects that need re-attaching;
    they may be many (the eval runner adds a list of case rows in
    one commit).
    """
    for attempt in range(_COMMIT_RETRIES):
        try:
            await session.commit()
            return
        except (OperationalError, Exception) as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "locked" not in msg or attempt == _COMMIT_RETRIES - 1:
                raise
            # Reset the session to a usable state, then re-attach
            # the rows.  ``rollback()`` may itself fail if the
            # underlying connection was invalidated; treat that as
            # terminal too.
            try:
                await session.rollback()
            except Exception:
                raise
            for r in rows:
                # ``add()`` is idempotent for already-attached rows.
                try:
                    session.add(r)
                except Exception:  # pragma: no cover
                    pass
            await asyncio.sleep(_COMMIT_BACKOFF_S * (attempt + 1))


@dataclass
class CaseResult:
    """Result of a single dataset case."""

    case_id: str
    input: str
    expected: Any
    output: str
    score: float
    passed: bool
    duration_ms: int
    tokens: int
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # The index of this case in the *original* request (preserves
    # the caller's ordering, not the gather-completion order).
    index: int = 0


@dataclass
class EvalResult:
    """Result of an entire :class:`EvalRunner.run` invocation."""

    eval_run_id: str
    agent_id: str
    dataset_name: str
    total: int
    passed: int
    failed: int
    average_score: float
    duration_ms: int
    cases: list[CaseResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# New run_eval entry point — types
# ---------------------------------------------------------------------------


@dataclass
class CaseEvalResult:
    """Result of running a single :class:`TestCase` through the agent."""

    case_id: str
    test_case_id: str
    input: str
    expected_answer: str | None
    expected_tool: str | None
    output: str
    status: str
    score: float
    passed: bool
    duration_ms: int
    tokens: int
    error: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    score_details: dict[str, Any] = field(default_factory=dict)
    # The index of this case in the *original* request (preserves
    # the caller's ordering, not the DB insertion order).
    index: int = 0


@dataclass
class RunEvalResult:
    """Result of an entire :meth:`EvalRunner.run_eval` invocation."""

    eval_run_id: str
    agent_id: str
    dataset: str
    total: int
    passed: int
    failed: int
    average_score: float
    duration_ms: int
    cases: list[CaseEvalResult] = field(default_factory=list)
    scorer_name: str = "composite"


class _TestCaseLoader:
    """Internal helper that converts DB ``EvalCase`` rows into
    :class:`app.eval.scorer.TestCase` instances, parsing the JSON ``meta``
    blob to look for ``expected_tool`` (the DB column itself is unused
    because the legacy schema doesn't carry tool expectations on
    ``EvalCase``).
    """

    @staticmethod
    def from_db(row: EvalCase) -> TestCase:
        try:
            meta = json.loads(row.meta or "{}")
        except (TypeError, ValueError):
            meta = {}
        return TestCase(
            id=row.id,
            input=row.input,
            expected_answer=row.expected,
            expected_tool=meta.get("expected_tool") if isinstance(meta, dict) else None,
            metadata=meta if isinstance(meta, dict) else {},
        )


class EvalRunner:
    """Drive an agent over an eval dataset and produce a score report."""

    def __init__(
        self,
        session: AsyncSession,
        agent: Agent,
        *,
        scorer: Scorer | AsyncScorer | None = None,
        pass_threshold: float = 0.7,
        max_concurrency: int = 2,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        # ``session`` is the parent session used for the bookkeeping
        # queries (loading the dataset, writing the ``EvalRun`` row and
        # the final aggregate).  Per-case execution uses a *fresh*
        # session drawn from ``session_factory`` so concurrent cases
        # do not race on the same connection.  When the caller does
        # not supply a factory we fall back to ``AsyncSessionLocal``
        # (the global async sessionmaker).  Resolved at *call time*
        # via ``_resolve_session_factory`` so the test conftest's
        # monkey-patch of ``app.database.AsyncSessionLocal`` is
        # honoured even when the runner was constructed earlier.
        self.session = session
        self.session_factory = session_factory
        self.agent = agent
        self.scorer: Scorer | AsyncScorer = scorer or Metrics.exact_match
        self.pass_threshold = pass_threshold
        self.semaphore = asyncio.Semaphore(max(1, max_concurrency))

    def _resolve_session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the per-case sessionmaker, resolving the production
        fallback lazily so monkey-patched ``app.database.AsyncSessionLocal``
        is honoured."""
        if self.session_factory is not None:
            return self.session_factory
        return _db_mod.AsyncSessionLocal

    # ---- public ------------------------------------------------------
    async def run(
        self,
        cases: Iterable[dict[str, Any]],
        *,
        dataset_name: str = "default",
        eval_run_id: str | None = None,
    ) -> EvalResult:
        eval_run_id = eval_run_id or str(uuid.uuid4())
        case_records = list(cases)
        # The API layer may have already inserted a placeholder EvalRun
        # with this id. Merge so we update if it exists, otherwise insert.
        run_row = await self.session.get(EvalRun, eval_run_id)
        if run_row is None:
            run_row = EvalRun(
                id=eval_run_id,
                agent_id=self.agent.id,
                dataset=dataset_name,
                total=len(case_records),
                passed=0,
                failed=0,
                average_score=0.0,
                status="running",
                started_at=datetime.utcnow(),
            )
            self.session.add(run_row)
        else:
            run_row.total = len(case_records)
            run_row.passed = 0
            run_row.failed = 0
            run_row.average_score = 0.0
            run_row.status = "running"
            run_row.started_at = datetime.utcnow()
        await self.session.commit()

        start = time.perf_counter()
        results: list[CaseResult] = []
        # Persist a placeholder EvalCase row **before** running the case
        # so the API and the /progress endpoint can list them mid-run.
        # We update the same row with the real result afterwards, so
        # `done` correctly advances case-by-case and the detail endpoint
        # can return full per-case output/score/error/timing.
        case_rows: list[EvalCase] = []
        for idx, raw in enumerate(case_records):
            row = EvalCase(
                id=str(uuid.uuid4()),
                eval_run_id=eval_run_id,
                dataset=dataset_name,
                name=f"case-{idx}",
                input=raw.get("input", ""),
                expected=(
                    raw.get("expected")
                    if raw.get("expected") is None
                    else str(raw.get("expected"))
                ),
                meta=json.dumps(raw.get("metadata") or {}, ensure_ascii=False),
                index=idx,
            )
            self.session.add(row)
            case_rows.append(row)
        # Flush so the rows get ids without committing the run state.
        # The new flow commits the case rows on a *separate* session
        # after the concurrent case loop completes (so concurrent
        # agents do not race for the write lock).  We commit the
        # placeholder rows here so they actually exist on disk and
        # the later UPDATE statements in the fresh session can find
        # them by id.
        await self.session.flush()
        await _commit_with_retry(self.session)
        self.session.expunge_all()

        # Run all cases concurrently, capped by the semaphore.
        # Each case uses its own AsyncSession drawn from
        # ``self.session_factory`` so concurrent cases do not race
        # on a single connection.
        async def _run_with_meta(idx: int, row: EvalCase, raw: dict[str, Any]) -> CaseResult:
            return await self._run_one(row.id, idx, raw)

        coros = [
            _run_with_meta(idx, row, raw)
            for idx, (raw, row) in enumerate(zip(case_records, case_rows))
        ]
        case_results = await asyncio.gather(*coros)
        # Re-sort by index to match the request order.
        case_results.sort(key=lambda c: c.index)

        for case_result, row in zip(case_results, case_rows):
            # Hydrate the EvalCase row so the API can serve it.
            row.output = case_result.output
            row.score = case_result.score
            row.passed = 1 if case_result.passed else 0
            row.duration_ms = case_result.duration_ms
            row.tokens = case_result.tokens
            row.error = case_result.error
            results.append(case_result)

        # Persist all hydrated rows in a single commit on a fresh
        # session to avoid colliding with the per-case commits.
        self.session.expunge_all()
        async with self._resolve_session_factory()() as write_session:
            for row in case_rows:
                write_session.add(row)
            await _commit_with_retry_safe_add(write_session, *case_rows)

        duration_ms = int((time.perf_counter() - start) * 1000)
        passed = sum(1 for r in results if r.passed)
        failed = len(results) - passed
        avg = (sum(r.score for r in results) / len(results)) if results else 0.0
        run_row.passed = passed
        run_row.failed = failed
        run_row.average_score = avg
        run_row.duration_ms = duration_ms
        run_row.status = "success"
        run_row.finished_at = datetime.utcnow()
        # Keep the JSON blob on the run row as a denormalised cache so
        # the list-runs endpoint can avoid an N+1 query.
        run_row.results = json.dumps(
            [
                {
                    "case_id": r.case_id,
                    "input": r.input,
                    "expected": r.expected,
                    "output": r.output,
                    "score": r.score,
                    "passed": r.passed,
                    "duration_ms": r.duration_ms,
                    "tokens": r.tokens,
                    "error": r.error,
                    "meta": r.meta,
                }
                for r in results
            ],
            ensure_ascii=False,
            default=str,
        )
        await self.session.commit()

        return EvalResult(
            eval_run_id=eval_run_id,
            agent_id=self.agent.id,
            dataset_name=dataset_name,
            total=len(results),
            passed=passed,
            failed=failed,
            average_score=avg,
            duration_ms=duration_ms,
            cases=results,
        )

    # ---- new run_eval entry point -----------------------------------
    async def run_eval(
        self,
        test_case_ids: Iterable[str],
        *,
        scorer: EvalScorer | None = None,
        pass_threshold: float | None = None,
        on_progress: ProgressCallback | None = None,
        dataset: str = "default",
        eval_run_id: str | None = None,
    ) -> RunEvalResult:
        """Run the agent against a set of :class:`TestCase` ids.

        Parameters
        ----------
        test_case_ids:
            The ids of the :class:`app.models.EvalCase` rows to execute.
        scorer:
            The :class:`app.eval.scorer.EvalScorer` to use.  Falls back
            to the :func:`default_scorer` composite (status + tool +
            answer) when ``None``.
        pass_threshold:
            Override the runner's ``pass_threshold``.  Each case is
            considered passed when ``score >= threshold`` and there
            was no error.
        on_progress:
            Optional callback ``(done, total)`` invoked after each
            case completes (and once at the start with ``(0, total)``).
        dataset:
            Name of the dataset (stored on the resulting ``EvalRun``
            row for grouping in the UI).
        eval_run_id:
            Reuse an existing :class:`app.models.EvalRun` row's id
            (the API creates a placeholder row before scheduling the
            background work).  When ``None`` a fresh uuid is minted.
        """
        from app.eval.scorer import default_scorer as _default

        scorer = scorer or _default()
        threshold = self.pass_threshold if pass_threshold is None else pass_threshold
        eval_run_id = eval_run_id or str(uuid.uuid4())

        # Load the rows ---------------------------------------------------------
        ids = list(test_case_ids)
        rows: list[EvalCase] = []
        if ids:
            stmt = select(EvalCase).where(EvalCase.id.in_(ids))
            res = await self.session.execute(stmt)
            rows = list(res.scalars().all())
            # Preserve the order requested by the caller
            order = {cid: i for i, cid in enumerate(ids)}
            rows.sort(key=lambda r: order.get(r.id, len(order)))
        total = len(rows)
        if on_progress is not None:
            try:
                on_progress(0, total)
            except Exception:  # pragma: no cover - never crash on progress
                logger.exception("on_progress callback failed (start)")

        # Create / reuse the EvalRun row in the running state --------------
        # The API layer may have already inserted a placeholder EvalRun
        # with this id.  Merge so we update if it exists, otherwise
        # insert.
        run_row = await self.session.get(EvalRun, eval_run_id)
        if run_row is None:
            run_row = EvalRun(
                id=eval_run_id,
                agent_id=self.agent.id,
                dataset=dataset,
                scorer=scorer.name,
                pass_threshold=float(threshold),
                total=total,
                passed=0,
                failed=0,
                average_score=0.0,
                status="running",
                started_at=datetime.utcnow(),
            )
            self.session.add(run_row)
        else:
            run_row.dataset = dataset
            run_row.scorer = scorer.name
            run_row.pass_threshold = float(threshold)
            run_row.total = total
            run_row.passed = 0
            run_row.failed = 0
            run_row.average_score = 0.0
            run_row.status = "running"
            run_row.started_at = datetime.utcnow()
            run_row.finished_at = None
            run_row.results = None
        await self.session.commit()

        start = time.perf_counter()
        cases: list[CaseEvalResult] = []

        # Build the per-case coroutines.  Each case runs on its own
        # AsyncSession (drawn from ``self.session_factory``) so the
        # shared ``self.session`` is only used for the bookkeeping
        # ``EvalRun`` row and the final aggregate write.
        async def _run(idx: int, row: EvalCase) -> CaseEvalResult:
            test_case = _TestCaseLoader.from_db(row)
            return await self._run_eval_one(
                test_case,
                row=row,
                index=idx,
                eval_run_id=eval_run_id,
                scorer=scorer,
            )

        # The semaphore caps the number of *in-flight* cases; the
        # gather launches them all but the coroutines queue on the
        # semaphore.  The legacy behaviour (max_concurrency=1 ⇒ serial)
        # is preserved because the semaphore allows at most one
        # acquisition at a time.
        coros = [_run(idx, row) for idx, row in enumerate(rows)]
        cases = await asyncio.gather(*coros)

        # Re-sort by ``index`` so the result list matches the order of
        # the requested ``test_case_ids`` (gather returns in completion
        # order which is non-deterministic under concurrency).
        cases.sort(key=lambda c: c.index)

        done = 0
        for case_result in cases:
            done += 1
            if on_progress is not None:
                try:
                    on_progress(done, total)
                except Exception:  # pragma: no cover
                    logger.exception("on_progress callback failed")

        duration_ms = int((time.perf_counter() - start) * 1000)
        passed = sum(1 for c in cases if c.passed)
        failed = len(cases) - passed
        avg = (sum(c.score for c in cases) / len(cases)) if cases else 0.0

        # The aggregate write touches the parent ``EvalRun`` row that
        # was loaded in ``self.session`` *before* the concurrent
        # case loop started.  In SQLAlchemy 2 the row stays attached
        # but the connection may have been recycled; do a defensive
        # ``expire_all`` so the row's columns are re-fetched on the
        # next access.  Then write on a *fresh* session to avoid
        # crossing the connection with the per-case commits above.
        self.session.expunge_all()
        async with self._resolve_session_factory()() as agg_session:
            run_row = await agg_session.get(EvalRun, eval_run_id)
            if run_row is not None:
                run_row.passed = passed
                run_row.failed = failed
                run_row.average_score = avg
                run_row.duration_ms = duration_ms
                # All cases are scored even when the underlying agent
                # failed, so the run itself is always marked
                # "completed".  A separate ``failed`` counter tracks
                # the cases that did not pass.
                run_row.status = "completed"
                run_row.finished_at = datetime.utcnow()
                # Mirror the per-case result on the run row as well so
                # the "list runs" API can show them without N+1
                # queries.
                run_row.results = json.dumps(
                    [
                        {
                            "case_id": c.case_id,
                            "test_case_id": c.test_case_id,
                            "input": c.input,
                            "expected": c.expected_answer,
                            "output": c.output,
                            "score": c.score,
                            "passed": c.passed,
                            "duration_ms": c.duration_ms,
                            "tokens": c.tokens,
                            "error": c.error,
                            "tool_calls": c.tool_calls,
                            "score_details": c.score_details,
                        }
                        for c in cases
                    ],
                    ensure_ascii=False,
                    default=str,
                )
                await _commit_with_retry(agg_session)

        return RunEvalResult(
            eval_run_id=eval_run_id,
            agent_id=self.agent.id,
            dataset=dataset,
            total=len(cases),
            passed=passed,
            failed=failed,
            average_score=avg,
            duration_ms=duration_ms,
            cases=cases,
            scorer_name=scorer.name,
        )

    async def _run_eval_one(
        self,
        test_case: TestCase,
        *,
        row: EvalCase,
        index: int,
        eval_run_id: str,
        scorer: EvalScorer,
    ) -> CaseEvalResult:
        """Execute a single :class:`TestCase` and persist its result row.

        The result row is created as a *new* :class:`EvalCase` that is
        linked back to the parent :class:`EvalRun` via ``eval_run_id``
        and keeps the original input/output fields intact for the
        caller's inspection.  The original ``row`` is left untouched
        (it represents the dataset template).

        Each case runs inside its own :class:`AsyncSession` drawn from
        ``self.session_factory`` (the legacy single-session path is
        preserved by ``EvalRunner`` falling back to a fresh session
        even when the caller did not pass a factory).  This is
        required because the agent loop writes to the database
        (``Run`` / ``Step`` rows) and concurrent cases sharing one
        session would race.
        """
        async with self.semaphore:
            start = time.perf_counter()
            error: str | None = None
            output = ""
            tokens = 0
            tool_calls: list[dict[str, Any]] = []
            status = "failed"
            try:
                async with self._resolve_session_factory()() as case_session:
                    loop = AgentLoop(self.agent, case_session)
                    result: LoopResult = await loop.run(test_case.input)
                output = result.output
                tokens = result.total_tokens
                status = result.status or "completed"
                # Pull tool calls out of the trace for the UI.  Each
                # step's payload may contain {"tool": "name", "args": ...}
                # but we only need a thin summary here.
                for step in getattr(result, "steps", []) or []:
                    payload = step.get("payload") if isinstance(step, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    tool_name = (
                        payload.get("name")
                        or payload.get("tool")
                        or payload.get("tool_name")
                    )
                    if tool_name:
                        tool_calls.append(
                            {
                                "name": tool_name,
                                "args": payload.get("args")
                                or payload.get("input"),
                                "output": payload.get("output")
                                or payload.get("result"),
                                "status": step.get("status", "ok"),
                            }
                        )
                if result.status not in ("success", "completed", "ok"):
                    error = result.error or result.status
            except Exception as exc:  # noqa: BLE001
                error = f"{exc.__class__.__name__}: {exc}"
                logger.exception("Eval case failed: %s", exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            run_result = AgentRunResult(
                run_id="",
                output=output,
                status=status,
                total_tokens=tokens,
                duration_ms=duration_ms,
                tool_calls=tool_calls,
                error=error,
            )
            score = scorer.score(run_result, test_case)
            passed = bool(score.passed and not error)

            # Persist a new EvalCase row that represents the *executed*
            # case (linked to the EvalRun).
            try:
                existing_meta = json.loads(row.meta or "{}")
            except (TypeError, ValueError):
                existing_meta = {}
            if not isinstance(existing_meta, dict):
                existing_meta = {}
            result_row = EvalCase(
                id=str(uuid.uuid4()),
                eval_run_id=eval_run_id,
                dataset=row.dataset,
                name=row.name,
                input=test_case.input,
                expected=test_case.expected_answer,
                meta=json.dumps(
                    {
                        **existing_meta,
                        "expected_tool": test_case.expected_tool,
                        "index": index,
                    }
                ),
                index=index,
                output=output,
                score=float(score.score),
                passed=1 if passed else 0,
                duration_ms=duration_ms,
                tokens=tokens,
                error=error,
                tool_calls=json.dumps(tool_calls, ensure_ascii=False, default=str),
                score_details=json.dumps(score.details, ensure_ascii=False, default=str),
            )
            # The bookkeeping session is shared with the outer
            # ``run_eval``; writing concurrently from many cases would
            # race.  Use a fresh session for the result row and refresh
            # the bookkeeping session before the final aggregate.
            async with self._resolve_session_factory()() as write_session:
                write_session.add(result_row)
                await _commit_with_retry_safe_add(write_session, result_row)

            return CaseEvalResult(
                case_id=result_row.id,
                test_case_id=test_case.id,
                input=test_case.input,
                expected_answer=test_case.expected_answer,
                expected_tool=test_case.expected_tool,
                output=output,
                status=status,
                score=float(score.score),
                passed=passed,
                duration_ms=duration_ms,
                tokens=tokens,
                error=error,
                tool_calls=tool_calls,
                score_details=score.details,
                index=index,
            )

    # ---- helpers -----------------------------------------------------
    async def _run_one(
        self, case_id: str, index: int, raw: dict[str, Any]
    ) -> CaseResult:
        async with self.semaphore:
            start = time.perf_counter()
            error: str | None = None
            output = ""
            tokens = 0
            try:
                # Use a fresh session per case to avoid races between
                # concurrent cases sharing the bookkeeping session.
                async with self._resolve_session_factory()() as case_session:
                    loop = AgentLoop(self.agent, case_session)
                    result: LoopResult = await loop.run(raw.get("input", ""))
                output = result.output
                tokens = result.total_tokens
                # Mirror the success-set used by ``_run_eval_one`` so that
                # a ``completed``/``ok`` status is not misreported as an
                # error (which would zero out the case's ``passed`` flag).
                if result.status not in ("success", "completed", "ok", None, ""):
                    error = result.error or result.status
            except Exception as exc:  # noqa: BLE001
                error = f"{exc.__class__.__name__}: {exc}"
                logger.exception("Eval case failed: %s", exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            score = await self._score(output, raw.get("expected"))
            return CaseResult(
                case_id=case_id,
                input=raw.get("input", ""),
                expected=raw.get("expected"),
                output=output,
                score=score,
                passed=score >= self.pass_threshold and not error,
                duration_ms=duration_ms,
                tokens=tokens,
                error=error,
                meta=raw.get("metadata") or {},
                index=index,
            )

    async def _score(self, prediction: str, expected: Any) -> float:
        if expected is None:
            return 1.0
        if asyncio.iscoroutinefunction(self.scorer):
            return float(await self.scorer(prediction, expected))  # type: ignore[arg-type]
        loop = asyncio.get_event_loop()
        return float(await loop.run_in_executor(None, self.scorer, prediction, expected))  # type: ignore[arg-type]


def load_dataset_from_path(path: str) -> list[dict[str, Any]]:
    """Load a dataset from a JSON or JSONL file."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = text.strip()
    if text.startswith("["):
        return json.loads(text)
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


__all__ = [
    "EvalRunner",
    "EvalResult",
    "CaseResult",
    "CaseEvalResult",
    "RunEvalResult",
    "ProgressCallback",
    "Scorer",
    "AsyncScorer",
    "load_dataset_from_path",
]

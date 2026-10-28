# Changelog

All notable changes to AgentForge are documented in this file.  The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

> **Repository layout note:** the backend lives in `backend/`.  When
> `pip install -e .` is used, the `agentforge` console script and the
> `app` Python package become available from any cwd.

## [Unreleased]

### Added
- **Tool risk badge in trace UI (#9).**  Every `tool_call` step now
  carries the calling tool's `risk` class on the wire (read from
  `ToolRegistry.spec(name).risk` in the agent loop, written to
  `payload["risk"]`, and surfaced as a top-level `risk` field on
  `StepOut` / the REST `GET /runs/{id}` body / the WebSocket
  `run.step` frame).  The frontend `StepNode.tsx` renders a coloured
  badge next to the tool name when the class is non-`safe`:
  `read` → green 📖, `write` → amber ✏️, `network` → orange 🌐,
  `exec` → red ⚡.  `safe` tools stay badge-free so the trace
  panel doesn't drown in green for the common case.  New CSS
  classes `.badge.warn-low / .warn / .warn-high / .danger` reuse
  the existing `--ok / --warn / --err` colour tokens.  10 new
  tests in `backend/tests/test_risk_badge.py` pin the contract at
  all three exit points (`StepOut`, `_serialize_step`,
  `_step_to_dict`) and snapshot the registry's risk classes for
  every built-in tool.

- **Tool metadata plugin layer (#3).**  A new `tool_metadata` table
  (alembic 0002) stores sparse runtime overrides of the source-of-
  truth tool registry in `app/agent/tools.py`.  Editable fields:
  `display_name`, `description`, `risk`, `scope`, `category`,
  `parameters_schema` (JSON), and an `enabled` flag that hides a
  tool from `GET /tools` and from the agent loop's prompt.  New
  HTTP surface (replaces the legacy `GET /tools` body so the
  /tools endpoint now reports `source: "registry" | "override"`):
  - `GET    /api/tools` -- merged view, registry defaults with DB
    override rows applied on top;
  - `GET    /api/tools/{name}` -- one tool;
  - `PUT    /api/tools/{name}/metadata` -- sparse upsert (missing
    fields keep the value from the existing row or the registry);
  - `DELETE /api/tools/{name}/metadata` -- drop the override
    (idempotent, 204 even if no row existed).

  The agent loop's `_step_once` reads the override table through
  `get_enabled_tool_names` (when a DB session is in scope) so a
  single `PUT` can disable a tool globally without re-saving every
  agent.  11 new tests in `tests/test_tools_metadata.py` cover the
  merge function, the DB-backed helper, and all four HTTP shapes
  (including 404 for unknown tools and idempotent delete).  The
  legacy `app.api.agents.tools_router` is preserved as a thin
  library helper so any old import keeps working.

- **Eval heatmap component (#4).**  A new front-end component
  (`src/components/EvalHeatmap.tsx`) renders one column per
  evaluation run and one row per unique test case, with each cell
  coloured by score (red 0 → green 1) and an ✕ glyph for failed
  cases.  Lazy-fetches per-run `cases` via `Evals.get` if the
  parent only fetched the list.  Embedded in
  `pages/Evaluation.tsx` above the "Past Evaluations" table and
  internationalised in `en.json` / `zh.json` (a new set of
  `eval.heatmap_*` keys).  No new dependencies.

- **Multipart dataset upload for evals (#2).**  `POST
  /api/v1/agents/{agent_id}/evals/upload` (also under `/api/...`)
  accepts a `.jsonl` or `.csv` file via `multipart/form-data` and
  hands the parsed cases to the existing `EvalRunner.run` pipeline
  with no behaviour change for the run, scoring, or persistence
  layers.  The endpoint is implemented in `app/api/evals.py`
  alongside the regular JSON create; the parsing lives in
  `app/eval/parser.py` (`parse_dataset`, `parse_jsonl`, `parse_csv`,
  `detect_format`, `DatasetParseError`).  Format is auto-detected
  from the upload filename (`.jsonl` / `.ndjson` / `.json` →
  JSONL, `.csv` → CSV) and falls back to a content sniff.  Each
  case is a `{input, expected?, metadata}` dict that the runner
  already accepts.  CSV treats `input / expected / name / tags` as
  reserved columns; any other column is folded into the case
  `metadata`.  `tags` accept either a JSON list or a `;`-separated
  string.  Errors are reported as HTTP 400 with per-line
  `ParseError` records, capped at 5 in the message preview and
  delivered in full via `detail.errors`.  3 new tests in
  `tests/test_evals_upload.py` (JSONL happy / CSV happy / malformed
  file → 400 with line numbers) plus 6 in
  `tests/test_parser.py` cover the dispatcher, both parsers, the
  `to_runner_payload` shape and the structured `DatasetParseError`
  path.


- **Live WebSocket trace stream (#10).**  `GET
  /api/agents/{agent_id}/runs/{run_id}/ws` (also under `/api/v1/...`)
  pushes each committed step to subscribed clients in real time so
  the front-end can show the agent "thinking" rather than waiting
  for the 1.5 s polling tick.  The protocol is documented in
  `app/api/ws.py`; four event shapes are emitted:

  - `run.snapshot` — full run + trace on connect (handy for clients
    joining mid-run or after a reconnect);
  - `run.step` — one frame per committed step (uses the same wire
    shape as `Runs.get().trace[]`);
  - `run.complete` — terminal frame carrying `status`, `output`,
    token counts and `step_count`;
  - `run.error` — pre-loop failure (e.g. agent not found).

  Implementation: a tiny in-process pub/sub (`RunBroadcaster` in
  `app/api/ws_broadcaster.py`) is fed by an
  `AgentLoop(..., config=LoopConfig(on_step=...))` callback
  installed in `app/api/runs.py::_execute_run` and `_resume_run`.
  Slow consumers are pruned (with a `__dropped__` sentinel) so a
  stalled WebSocket can never back-pressure the agent loop.
  Front-end hook in `src/hooks/useRunStream.ts`; `AgentRun.tsx`
  merges `run.step` frames into the existing trace list and falls
  back to polling on unexpected close.  7 new tests in
  `tests/test_ws.py` (4 broadcaster unit + 3 ws endpoint
  integration).

- **Memory periodic cleanup on lifespan.**  The FastAPI
  `lifespan` in `app/main.py` now spawns
  `app.memory.cleanup.schedule_periodic_cleanup` on startup and
  cancels it on shutdown, so an installed AgentForge instance
  no longer depends on a test or CLI command to start the
  background TTL sweep.  The interval comes from the new
  `AGENTFORGE_MEMORY_CLEANUP_INTERVAL_SECONDS` setting
  (default 300 s; ``0`` disables).  1 new lifespan test is
  documented in `tests/test_main_lifespan.py` (added in 0.1.3).

- **Calculator sandbox split (#7, 0.1.2 round 7 close-out).**
  The `calculator` tool is now an `async` coroutine that runs
  the (validated) arithmetic expression through the configured
  async sandbox (`LocalSandbox` subprocess or `DockerSandbox`
  container) when `AGENTFORGE_SANDBOX_ENABLED=true`, mirroring
  what `shell_exec` already did.  When the sandbox is off
  (0.1.x default) the function falls back to the historical
  `eval()` under `__builtins__={}` after a strict
  `0-9+-*/().%` allow-list check, so the regression surface
  for tests that do not enable the sandbox is unchanged.  3
  new tests in `tests/test_tools.py` cover both paths and the
  allow-list rejection.

- **Python expression evaluator (`python_eval`, 0.1.3 #1).**
  New general-purpose tool that supersedes `calculator` for any
  non-trivial arithmetic.  Three modes are exposed, each a
  strict superset of the previous one:

  - `simple`  — same allow-list as `calculator` (digits +
    arithmetic only).  This is what `calculator` now delegates
    to internally, so the legacy contract is preserved bit-for-
    bit and `test_calculator_*` tests continue to pass.
  - `math`    — full Python expressions with `math` and
    `statistics` modules pre-imported.  An AST gate
    (`_MathModeValidator` in `app/agent/tools.py`) rejects
    `import` statements (caught by `ast.parse` in
    `mode="eval"` as syntax), any `__dunder__` attribute
    access, and calls to forbidden builtins (`open`,
    `exec`, `eval`, `__import__`, `globals`, `getattr`, etc.).
  - `sandbox` — same AST-gated expression, but the actual
    evaluation runs in a fresh Python subprocess via
    `get_sandbox()` when `AGENTFORGE_SANDBOX_ENABLED=true`.
    Falls back to in-process `math` mode when the sandbox is
    off, so the safety guarantee is identical either way.

  4 new tests in `tests/test_tools.py` cover the simple /
  math / sandbox paths and the AST gate.  `calculator` still
  works for backward compatibility — the function is now a
  one-liner that delegates to `python_eval(expr, "simple")`.

### Changed
- `DELETE /api/agents/{agent_id}` and
  `DELETE /api/agents/{agent_id}/memory/{memory_id}` now return
  HTTP 204 No Content (no body) instead of HTTP 200 with a
  `{"ok": true, "deleted_id": ...}` envelope, matching the
  REST contract exercised by `test_agent_lifecycle`.

### Fixed
- **Inline-case evaluations now persist their terminal summary.**  The legacy
  `EvalRunner.run` path writes `completed`, pass/fail counts, average score,
  timing, and the per-case cache through a fresh session after expunging the
  per-case rows.  The evaluation UI now reads the API's `dataset` /
  `average_score` contract, calculates pass rate from passed / total, and
  refreshes history summaries when a terminal run is opened.  A regression
  test verifies the aggregate from a separate database session.

- `on_conflict="error"` in the import endpoint already raised
  HTTP 409; the previous CHANGELOG note flagged it as still
  returning 400 but that was a stale observation — the
  `test_import_error_on_conflict_aborts` test asserts 409 and
  has been green since 0.1.2 round 9.  No code change.

- **`test_memory_vector.py::TestVectorStoreProtocol::test_unknown_backend_raises`**
  was failing on 0.1.2 because the test patched `VECTOR_BACKEND` while
  `Settings` uses `env_prefix="AGENTFORGE_"` (so the real env var is
  `AGENTFORGE_VECTOR_BACKEND`).  Renamed the patched env var and the
  surrounding comment.  No production code change.  Whole file
  (`backend/tests/test_memory_vector.py`, 24 tests) is now green.

- **`Settings.llm_provider` Literal now accepts all 5 documented
  providers** (`mock` / `openai` / `anthropic` / `deepseek` /
  `qwen`).  Previously the `Literal["openai", "anthropic",
  "mock"]` would reject `AGENTFORGE_LLM_PROVIDER=deepseek` /
  `=qwen` at pydantic-parse time even though
  `AgentLoop._build_default_llm` already routed those two
  providers to the OpenAI-compatible preset.  The field is also
  renamed `default_provider` → `llm_provider` so the
  pydantic-settings env name (`AGENTFORGE_LLM_PROVIDER`) actually
  matches what the README documents and what `loop.py` reads.
  9 new tests in `tests/test_config.py` cover all 5 provider
  values, the `weaviate`-rejection path, and env-var
  overrides for `deepseek` / `qwen`.

## [0.1.2] - 2026-09-03

0.1.2 is the "everything works in earnest" release.  Eight rounds of work
folded long-term memory, evals, prompts, tools, vector search, TTL, Alembic
migrations, and import/export into a single coherent backend.  All changes
are additive; the 0.1.0 HTTP surface is preserved.

### Added
- **Long-term memory (#1, 0.1.2 round 1).** `Memory` rows with embedding
  bytes + session id, plus `MemoryStore` CRUD that always goes through
  the configured vector store.  Default embedder is a hashing-trick
  in-process implementation (`app/memory/embeddings.py`) so no extra
  dependency is required.
- **Eval harness with concurrent workers (#2, 0.1.2 round 5.1).**
  `EvalRunner.run_eval` now runs cases in parallel via
  `asyncio.gather + Semaphore(max_concurrency)`, each case gets its own
  `AsyncSession` (a shared session deadlocked N agent loops), and
  aggregate writes use a separate session.  `max_concurrency` defaults
  to 4 and is exposed on the API schema.
- **Prompts split out of code (#3, 0.1.2 round 3).** System and per-role
  prompts live in `app/agent/prompts.py` (plain Python strings, not
  templated) so editing prose no longer requires a code review.
- **Tool registry and metrics (#4, 0.1.2 round 4).** Tools are registered
  declaratively, with a `metrics.py` module exposing per-call counters
  and timing histograms for the LLM, tools, and the agent loop.
- **Vector backends (#6, 0.1.2 round 4.2).** `app/memory/vector_store.py`
  defines a `VectorStore` Protocol with an `InMemoryVectorStore` default
  and a factory keyed on `AGENTFORGE_VECTOR_BACKEND` (ChromaDB and
  pgvector are optional dependencies; both ship as
  `pip install agentforge[vector]` extras when wired).
- **Memory TTL (#6, 0.1.2 round 4.3).** `Memory.expires_at` is indexed;
  `MemoryStore.add(ttl_seconds=...)` computes the expiry on write; the
  three search modes (`exact`, `embed`, `hybrid`) all hide expired
  rows; `app.memory.cleanup.cleanup_expired` deletes in batches and
  `schedule_periodic_cleanup` runs every five minutes as a background
  task.
- **Alembic migrations (#8, 0.1.2 round 8).** `alembic/`, `alembic.ini`
  and `versions/0001_baseline.py` autogenerate a baseline for all six
  models (`agents`, `agent_runs`, `agent_steps`, `memories`,
  `eval_cases`, `eval_runs`) with FK CASCADE + the full set of indexes.
  `app/database.py:init_db` is replaced by `alembic upgrade head`
  (via `asyncio.to_thread(command.upgrade)`); `app/config.py` adopts
  `env_prefix="AGENTFORGE_"` so the pre-0.1.2 `AGENTFORGE_DATABASE_URL`
  env var finally gets read.
- **Agent import / export (#7, 0.1.2 round 9).** New
  `app/agent/exporter.py` (pure logic) plus `POST /api/v1/agents/import`
  and `POST /api/v1/agents/export`.  Versioned envelope
  (`version=1`, `kind=agentforge.agent`) is forward-compatible (unknown
  fields are dropped on load).  Supports JSON and YAML, detected from
  filename or the `---` magic header.  `pyyaml==6.0.3` added to the
  dependency list.
- **`agentforge` CLI.** `pip install -e .` exposes an `agentforge`
  console script with three subcommands: `version`, `serve` (uvicorn
  wrapper), and `migrate` (runs `alembic upgrade head` against the
  configured database).
- **Packaging metadata.** `pyproject.toml` (PEP 621) declares the
  project; `LICENSE` (MIT) and this `CHANGELOG.md` ship at the repo
  root; the version constant lives in `app/__init__.py` as the single
  source of truth and is re-exported by `app.__version__` for the CLI.

### Changed
- `Memory` table gained two columns: `embedding` (LargeBinary) and
  `session_id` (String(36)).
- `app/config.py` now uses pydantic-settings' `env_prefix`; every
  previous `AGENTFORGE_*` env var continues to work, but
  `AGENTFORGE_DATABASE_URL` now actually gets read.
- `app/database.py:init_db` no longer creates tables via
  `Base.metadata.create_all`; the schema is now the responsibility of
  Alembic.
- `requirements.txt` kept for backwards-compat workflows; the canonical
  dependency list now lives in `pyproject.toml`.  Run
  `pip install -e ".[dev]"` for the dev extras.

### Fixed
- 0.1.1's `EvalRunner` shared a single `AsyncSession` across N agent
  loops, which deadlocked on the first pool exhaustion.  0.1.2's
  per-case session model is race-free up to `max_concurrency=64`
  (empirically) on SQLite.
- `AGENTFORGE_DATABASE_URL` was previously ignored by pydantic-settings
  because `env_prefix` was missing; set in 0.1.2.

### Test summary
- 221 passed, 3 pre-existing failures
  (`test_agent_lifecycle` 200-vs-204,
  `test_periodic_cleanup_runs_and_can_be_cancelled` (in-memory SQLite
  timing), and
  `TestVectorStoreProtocol.test_unknown_backend_raises`) that are not
  regressions of this release — all three are documented in
  `README.md` as "known pre-existing" and tracked for 0.1.3.

## [0.1.1] - 2025-12-XX

Pre-public iteration.  No changelog kept.

## [0.1.0] - 2025-11-XX

Initial public cut.  Single-agent loop, six tools (`shell_exec`,
`http_get`, `read_file`, `write_file`, `search_knowledge`,
`calculator`), SQLite via `aiosqlite`, JWT auth, no long-term memory,
no evals, no import/export.  `README.md` describes that baseline in the
"工具沙箱" and "数据库" sections.

[Unreleased]: https://example.invalid/agentforge/compare/0.1.2...HEAD
[0.1.2]: https://example.invalid/agentforge/compare/0.1.1...0.1.2
[0.1.1]: https://example.invalid/agentforge/compare/0.1.0...0.1.1
[0.1.0]: https://example.invalid/agentforge/releases/tag/0.1.0
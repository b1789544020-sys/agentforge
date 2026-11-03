"""Celery application and durable Agent/Eval task entry points."""
from __future__ import annotations

import asyncio

from celery import Celery
from sqlalchemy import select

from app.config import get_settings

settings = get_settings()
celery_app = Celery(
    "agentforge",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks"],
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_time_limit=settings.celery_task_time_limit_seconds,
    task_soft_time_limit=max(30, settings.celery_task_time_limit_seconds - 30),
    broker_transport_options={"visibility_timeout": settings.celery_task_time_limit_seconds + 300},
    result_expires=24 * 3600,
    beat_schedule={
        "memory-ttl-cleanup": {"task": "agentforge.memory_cleanup", "schedule": 300.0},
        "stuck-run-watchdog": {"task": "agentforge.stuck_watchdog", "schedule": 60.0},
    },
)


async def _execute_agent_run(run_id: str) -> None:
    import uuid

    from app.api.runs import _execute_run, _resume_run
    from app.database import async_session_factory
    from app.infra.redis import get_redis
    from app.models import Run

    redis = get_redis()
    lock_key = f"agentforge:task-lock:run:{run_id}"
    owner = uuid.uuid4().hex
    claimed = await redis.set(
        lock_key, owner, nx=True, ex=settings.celery_task_time_limit_seconds + 300
    )
    if not claimed:
        return
    try:
        async with async_session_factory() as session:
            run = await session.get(Run, run_id)
            if run is None or run.status in {"success", "failed", "cancelled", "terminated"}:
                return
            input_ = run.input
            agent_id = run.agent_id
            session_id = run.session_id
            resume = bool(run.state)
            if resume:
                run.status = "paused"
                await session.commit()
        if resume:
            await _resume_run(run_id, agent_id, session_id, input_)
        else:
            await _execute_run(run_id, agent_id, input_, session_id)
    finally:
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
            1, lock_key, owner,
        )


@celery_app.task(name="agentforge.execute_run")
def execute_run_task(run_id: str) -> None:
    asyncio.run(_execute_agent_run(run_id))


@celery_app.task(name="agentforge.resume_run")
def resume_run_task(run_id: str) -> None:
    asyncio.run(_execute_agent_run(run_id))


@celery_app.task(name="agentforge.execute_eval")
def execute_eval_task(eval_run_id: str) -> None:
    import uuid

    from app.eval.service import execute_eval
    from app.infra.redis import get_redis

    async def execute() -> None:
        redis = get_redis()
        key = f"agentforge:task-lock:eval:{eval_run_id}"
        owner = uuid.uuid4().hex
        claimed = await redis.set(
            key, owner, nx=True, ex=settings.celery_task_time_limit_seconds + 300
        )
        if not claimed:
            return
        try:
            await execute_eval(eval_run_id)
        finally:
            await redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
                1, key, owner,
            )

    asyncio.run(execute())


@celery_app.task(name="agentforge.memory_cleanup")
def memory_cleanup_task() -> None:
    from app.agent.memory import cleanup_expired
    from app.database import async_session_factory

    async def cleanup() -> None:
        async with async_session_factory() as session:
            await cleanup_expired(session)

    asyncio.run(cleanup())


@celery_app.task(name="agentforge.stuck_watchdog")
def stuck_run_watchdog_task() -> None:
    from datetime import datetime, timedelta

    from app.database import async_session_factory
    from app.models import EvalRun, Run

    async def watchdog() -> None:
        cutoff = datetime.utcnow() - timedelta(
            seconds=settings.celery_task_time_limit_seconds
        )
        async with async_session_factory() as session:
            runs = (
                await session.execute(
                    select(Run).where(Run.status == "running", Run.started_at < cutoff)
                )
            ).scalars().all()
            evals = (
                await session.execute(
                    select(EvalRun).where(
                        EvalRun.status.in_(["pending", "running"]),
                        EvalRun.started_at < cutoff,
                    )
                )
            ).scalars().all()
            for row in [*runs, *evals]:
                row.status = "failed"
                row.finished_at = datetime.utcnow()
                if isinstance(row, Run):
                    row.error = "worker time limit exceeded"
            await session.commit()

    asyncio.run(watchdog())


async def recover_interrupted_runs() -> int:
    """Requeue persisted unfinished runs after an API process restart."""
    from app.database import async_session_factory
    from app.dispatch import submit_resume, submit_run
    from app.models import Run

    if settings.control_backend == "redis":
        from app.infra.redis import get_redis

        claimed = await get_redis().set(
            "agentforge:recovery:startup-lock", "1", nx=True, ex=300
        )
        if not claimed:
            return 0
    recovered = 0
    async with async_session_factory() as session:
        rows = (
            await session.execute(select(Run).where(Run.status.in_(["pending", "running"])))
        ).scalars().all()
        for run in rows:
            if run.state:
                run.status = "paused"
                await session.commit()
                submit_resume(run.id, run.agent_id)
            else:
                submit_run(run.id, run.agent_id, run.input, run.session_id)
            recovered += 1
    return recovered


__all__ = [
    "celery_app", "execute_run_task", "resume_run_task", "execute_eval_task",
    "recover_interrupted_runs",
]

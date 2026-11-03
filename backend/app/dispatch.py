"""Select durable Celery dispatch or the explicit local-development backend."""
from __future__ import annotations

import asyncio

from fastapi import BackgroundTasks

from app.config import get_settings


def submit_run(
    run_id: str, agent_id: str, user_input: str, session_id: str | None,
    background: BackgroundTasks | None = None,
) -> None:
    if get_settings().task_backend == "celery":
        from app.tasks import execute_run_task

        execute_run_task.delay(run_id)
        return
    from app.api.runs import _execute_run

    if background is not None:
        background.add_task(_execute_run, run_id, agent_id, user_input, session_id)
    else:
        asyncio.get_running_loop().create_task(
            _execute_run(run_id, agent_id, user_input, session_id)
        )


def submit_resume(
    run_id: str, agent_id: str, background: BackgroundTasks | None = None
) -> None:
    if get_settings().task_backend == "celery":
        from app.tasks import resume_run_task

        resume_run_task.delay(run_id)
        return
    from app.api.runs import _resume_run
    from app.database import async_session_factory
    from app.models import Run

    async def resume() -> None:
        async with async_session_factory() as session:
            run = await session.get(Run, run_id)
            if run is not None:
                await _resume_run(run_id, agent_id, run.session_id, run.input)

    if background is not None:
        background.add_task(resume)
    else:
        asyncio.get_running_loop().create_task(resume())


def submit_eval(eval_run_id: str, background: BackgroundTasks) -> None:
    if get_settings().task_backend == "celery":
        from app.tasks import execute_eval_task

        execute_eval_task.delay(eval_run_id)
        return
    from app.eval.service import execute_eval

    background.add_task(execute_eval, eval_run_id)


__all__ = ["submit_run", "submit_resume", "submit_eval"]

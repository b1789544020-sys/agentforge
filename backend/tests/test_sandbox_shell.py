"""Tests for the shell sandbox driver and the ``shell_exec`` tool wiring.

These tests focus on the new ``run_shell`` entry point that lives on
both :class:`LocalSandbox` and :class:`DockerSandbox`, plus the routing
inside :func:`app.agent.tools.shell_exec` (the tool now optionally
forwards to the configured async sandbox when
``AGENTFORGE_SANDBOX_ENABLED`` is true).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys

import pytest

from app.agent import sandbox as sandbox_mod
from app.agent.sandbox import (
    LocalSandbox,
    SandboxError,
    SandboxResult,
    get_sandbox,
    reset_sandbox,
)
from app.agent.tools import shell_exec

# 本模块 spawn 真实子进程（python -c / cmd builtin），机器负载高时进程
# 启动时序会漂；失败时透明重跑几次（全过时不增加任何耗时）。
pytestmark = pytest.mark.flaky(reruns=3, reruns_delay=0.5)


# ---------------------------------------------------------------------------
# LocalSandbox.run_shell
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_shell_success_returns_stdout_and_exit_code() -> None:
    """A real ``python -c "print(42)"`` call should return exit 0 + stdout."""
    sb = LocalSandbox()
    if sys.platform == "win32":
        cmd, args = "python", ["-c", "print(42)"]
    else:
        cmd, args = "python3", ["-c", "print(42)"]
    res = await sb.run_shell(cmd, args, timeout_s=10)
    assert isinstance(res, SandboxResult)
    assert res.exit_code == 0
    assert "42" in res.stdout
    assert res.stderr == ""


@pytest.mark.asyncio
async def test_run_shell_nonzero_exit_code_is_propagated() -> None:
    """A failing command should return non-zero exit_code, not raise."""
    sb = LocalSandbox()
    if sys.platform == "win32":
        cmd, args = "cmd", ["/c", "exit", "7"]
    else:
        cmd, args = "sh", ["-c", "exit 7"]
    res = await sb.run_shell(cmd, args, timeout_s=10)
    assert res.exit_code == 7


@pytest.mark.asyncio
async def test_run_shell_timeout_raises_sandbox_error() -> None:
    """A command exceeding ``timeout_s`` must be killed and raise SandboxError."""
    sb = LocalSandbox()
    if sys.platform == "win32":
        cmd, args = "ping", ["-n", "99", "127.0.0.1"]
    else:
        cmd, args = "sleep", ["30"]
    with pytest.raises(SandboxError) as exc:
        await sb.run_shell(cmd, args, timeout_s=0.5)
    assert "timed out" in str(exc.value).lower()





@pytest.mark.asyncio
async def test_run_shell_missing_executable_does_not_crash() -> None:
    """A non-existent binary should not raise; result should indicate failure.

    On POSIX ``shell=False`` raises ``FileNotFoundError`` which we surface
    as :class:`SandboxError`.  On Windows the OS instead returns exit
    code 1 with a friendly "not recognized" message on stderr.  Either
    way the caller must receive a structured result they can act on.
    """
    sb = LocalSandbox()
    res = await sb.run_shell("definitely_not_a_real_binary_xyz_123", [], timeout_s=5)
    if sys.platform == "win32":
        # cmd /c fallback returns non-zero + "not recognized" on stderr.
        assert res.exit_code != 0
        assert "not recognized" in res.stderr.lower() or "not found" in res.stderr.lower()
    else:
        # POSIX: FileNotFoundError -> SandboxError surfaces a clean message.
        assert res.exit_code != 0
        assert "not found" in res.stderr.lower()


@pytest.mark.asyncio
async def test_run_shell_cwd_is_honored() -> None:
    """``cwd`` should change the working directory of the child process."""
    sb = LocalSandbox()
    if sys.platform == "win32":
        cmd, args = "python", ["-c", "import os; print(os.getcwd())"]
    else:
        cmd, args = "python3", ["-c", "import os; print(os.getcwd())"]
    target = os.path.dirname(os.path.abspath(__file__))
    res = await sb.run_shell(cmd, args, timeout_s=10, cwd=target)
    assert res.exit_code == 0
    assert os.path.realpath(target).lower() in res.stdout.lower()


# ---------------------------------------------------------------------------
# shell_exec tool (legacy/default path — sandbox disabled)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shell_exec_runs_python_returning_stdout() -> None:
    """The default driver must run a real command and return its stdout."""
    if sys.platform == "win32":
        cmd, args = "python", ["-c", "print('hello-from-shell')"]
    else:
        cmd, args = "python3", ["-c", "print('hello-from-shell')"]
    result = await shell_exec(cmd, args=args, timeout_s=10)
    assert result["exit_code"] == 0
    assert "hello-from-shell" in result["stdout"]
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_shell_exec_nonzero_exit_code_is_returned_not_raised() -> None:
    """A failing command should yield a result dict, not an exception."""
    if sys.platform == "win32":
        cmd, args = "cmd", ["/c", "exit 3"]
    else:
        cmd, args = "sh", ["-c", "exit 3"]
    result = await shell_exec(cmd, args=args, timeout_s=10)
    assert result["exit_code"] == 3
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_shell_exec_missing_executable_returns_error_dict() -> None:
    """A missing binary should return a structured result dict, not raise.

    On POSIX the default driver raises ``FileNotFoundError`` which the
    tool catches and surfaces as ``exit_code=-1``.  On Windows the cmd
    fallback returns exit code 1 with a friendly error message — both
    are valid structured error responses, neither raises.
    """
    result = await shell_exec(
        "definitely_not_a_real_binary_abc_456", args=[], timeout_s=5
    )
    if sys.platform == "win32":
        assert result["exit_code"] != 0
        assert result["timed_out"] is False
        assert (
            "not recognized" in result["stderr"].lower()
            or "not found" in result["stderr"].lower()
        )
    else:
        assert result["exit_code"] == -1
        assert (
            "not found" in result["stderr"].lower()
            or result["timed_out"] is True
        )


# ---------------------------------------------------------------------------
# Protocol + factory plumbing
# ---------------------------------------------------------------------------
def test_async_sandbox_protocol_declares_run_shell() -> None:
    """Both run_python and run_shell must be part of the protocol."""
    proto = sandbox_mod._AsyncSandbox
    members = set(getattr(proto, "__annotations__", {}).keys()) | set(dir(proto))
    assert "run_shell" in members
    assert "run_python" in members


@pytest.mark.asyncio
async def test_local_sandbox_satisfies_async_protocol() -> None:
    """LocalSandbox must expose both ``run_python`` and ``run_shell`` as coros."""
    sb = LocalSandbox()
    assert inspect.iscoroutinefunction(sb.run_python)
    assert inspect.iscoroutinefunction(sb.run_shell)


@pytest.mark.asyncio
async def test_get_sandbox_returns_local_when_disabled(monkeypatch) -> None:
    """With sandbox_enabled=False, get_sandbox() must return LocalSandbox."""
    await reset_sandbox()

    class _FakeSettings:
        sandbox_enabled = False
        sandbox_image = "python:3.12-slim"
        sandbox_memory_limit = "256m"
        sandbox_cpu_quota = 1.0
        sandbox_network_disabled = True
        sandbox_timeout_seconds = 30

    monkeypatch.setattr(sandbox_mod, "get_settings", lambda: _FakeSettings())
    sb = get_sandbox()
    assert isinstance(sb, LocalSandbox)
    await reset_sandbox()


@pytest.mark.asyncio
async def test_get_sandbox_falls_back_to_local_when_docker_unavailable(
    monkeypatch,
) -> None:
    """With sandbox_enabled=True but no Docker, get_sandbox() falls back to Local."""
    await reset_sandbox()

    class _FakeSettings:
        sandbox_enabled = True
        sandbox_image = "python:3.12-slim"
        sandbox_memory_limit = "256m"
        sandbox_cpu_quota = 1.0
        sandbox_network_disabled = True
        sandbox_timeout_seconds = 30

    monkeypatch.setattr(sandbox_mod, "get_settings", lambda: _FakeSettings())
    from app.agent import sandbox as sm

    def _boom(*_a, **_kw):
        raise SandboxError("docker not available (test stub)")

    monkeypatch.setattr(sm, "DockerSandbox", _boom)
    sb = get_sandbox()
    assert isinstance(sb, LocalSandbox)
    await reset_sandbox()

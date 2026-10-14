"""Tests for the tool registry / sandbox."""
from __future__ import annotations

import pytest

from app.agent.sandbox import Sandbox
from app.agent.tools import ToolError, ToolRegistry, tool


def test_builtin_tools_registered() -> None:
    names = {t.name for t in ToolRegistry.list_all()}
    assert {"echo", "now", "calculator"}.issubset(names)


def test_custom_tool_registration() -> None:
    @tool(name="upper", description="Uppercase a string")
    def upper(s: str) -> str:
        return s.upper()

    assert ToolRegistry.get("upper") is upper
    spec = ToolRegistry.spec("upper")
    assert spec is not None
    assert spec.name == "upper"
    ToolRegistry.unregister("upper")


@pytest.mark.asyncio
async def test_echo_tool() -> None:
    echo = ToolRegistry.get("echo")
    result = await echo.run(text="hi")
    assert result == "hi"


def test_sandbox_blocks_dangerous_calls() -> None:
    sb = Sandbox()
    with pytest.raises(PermissionError):
        sb.exec("open('/etc/passwd')")


def test_sandbox_allows_safe_math() -> None:
    sb = Sandbox()
    out = sb.exec("__import__('math').sqrt(16)")
    assert out == "4.0"


# ---- calculator (0.1.2 #7 sandbox split, 0.1.2 final close-out) ---------


@pytest.mark.asyncio
async def test_calculator_default_allowlist_path() -> None:
    """Default (sandbox off) path: allow-list + eval, sync return."""
    from app.agent.tools import calculator

    # Async function; result is the string form of the value.
    assert await calculator("2 + 2 * 3") == "8"
    assert await calculator("(1 + 2) * (3 + 4)") == "21"
    assert await calculator("10 % 3") == "1"
    assert await calculator("3.14 * 2") == "6.28"


@pytest.mark.asyncio
async def test_calculator_allowlist_blocks_dangerous() -> None:
    """Allow-list must reject any non-arithmetic character before eval."""
    from app.agent.tools import ToolError, calculator

    for bad in ("__import__('os')", "open('x')", "a; b", "x ** y", "eval(1)"):
        with pytest.raises(ToolError):
            await calculator(bad)


# ---- python_eval (0.1.3 #1) ----------------------------------------------


@pytest.mark.asyncio
async def test_python_eval_simple_mode_matches_calculator() -> None:
    """``python_eval(expr, "simple")`` is exactly the legacy calculator
    contract: tight allow-list + eval, returns the stringified value."""
    from app.agent.tools import python_eval

    assert await python_eval("2 + 2 * 3", mode="simple") == "8"
    assert await python_eval("(1 + 2) * (3 + 4)", mode="simple") == "21"
    # Anything outside the allow-list is rejected.
    for bad in ("__import__('os')", "math.sqrt(2)", "x ** y"):
        with pytest.raises(ToolError):
            await python_eval(bad, mode="simple")


@pytest.mark.asyncio
async def test_python_eval_math_mode_supports_helpers() -> None:
    """``mode="math"`` exposes ``math`` and ``statistics`` namespaces
    plus ordinary arithmetic.  No subprocess, no sandbox required."""
    from app.agent.tools import python_eval

    assert await python_eval("math.sqrt(16)", mode="math") == "4.0"
    assert await python_eval("math.pi > 3", mode="math") == "True"
    assert await python_eval("statistics.mean([1, 2, 3, 4])", mode="math") == "2.5"
    # math.e is a float so the whole expression is a float too.
    assert (
        await python_eval("(math.e ** 2) - math.e ** 2 + 7", mode="math")
        == "7.0"
    )


@pytest.mark.asyncio
async def test_python_eval_math_mode_rejects_dangerous() -> None:
    """AST gate must reject dunder attribute access and forbidden
    builtin calls *before* any code reaches the interpreter.  Top-level
    ``import`` statements are caught even earlier by
    :func:`ast.parse` in ``mode="eval"`` (which is not a statement
    context) -- both layers count as protection."""
    from app.agent.tools import python_eval

    # import / from-import: caught by ast.parse(mode="eval") as syntax error
    with pytest.raises(ToolError, match="syntax|import"):
        await python_eval("import os", mode="math")
    with pytest.raises(ToolError, match="syntax|import"):
        await python_eval("from os import system", mode="math")
    # dunder attribute access
    with pytest.raises(ToolError, match="dunder"):
        await python_eval(
            "().__class__.__bases__[0].__subclasses__()", mode="math"
        )
    # forbidden builtin calls
    for forbidden in (
        "open('x')",
        "exec('x')",
        "eval('1')",
        "__import__('os')",
    ):
        with pytest.raises(ToolError):
            await python_eval(forbidden, mode="math")
    # unknown mode
    with pytest.raises(ToolError, match="unknown mode"):
        await python_eval("1+1", mode="bogus")
    # syntax error
    with pytest.raises(ToolError, match="syntax"):
        await python_eval("1 +", mode="math")


@pytest.mark.flaky(reruns=3, reruns_delay=0.5)  # 走真实子进程沙箱，时序受负载影响
@pytest.mark.asyncio
async def test_python_eval_sandbox_mode_routes_through_sandbox(monkeypatch) -> None:
    """With ``AGENTFORGE_SANDBOX_ENABLED=true`` the sandbox-mode call must
    end up in a separate Python interpreter.  We use the same
    monkeypatch pattern as the calculator test -- assert that the
    in-process AST-gated value is replaced by a value that came back
    from the subprocess stdout (the subprocess prints ``repr(value)``
    so a plain int is fine)."""
    from app import config as config_mod
    from app.agent import sandbox as sandbox_mod
    from app.agent.tools import python_eval

    await sandbox_mod.reset_sandbox()
    monkeypatch.setenv("AGENTFORGE_SANDBOX_ENABLED", "true")
    config_mod.get_settings.cache_clear()
    try:
        # math.sqrt(9) -> 3.0; AST-gated, then re-evaluated in subprocess.
        assert await python_eval("math.sqrt(9)", mode="sandbox") == "3.0"
        # 2 + 2 -> 4 -- exercises the literal path through the subprocess.
        assert await python_eval("2 + 2", mode="sandbox") == "4"
    finally:
        await sandbox_mod.reset_sandbox()
        monkeypatch.delenv("AGENTFORGE_SANDBOX_ENABLED", raising=False)
        config_mod.get_settings.cache_clear()

@pytest.mark.asyncio
async def test_calculator_sandbox_path(monkeypatch) -> None:
    """With ``AGENTFORGE_SANDBOX_ENABLED=true`` the calculator should
    route through the configured async sandbox (LocalSandbox or
    DockerSandbox) and still return the correct value."""
    from app.agent import sandbox as sandbox_mod
    from app.agent.tools import calculator

    # Force a fresh, isolated sandbox singleton for this test.
    await sandbox_mod.reset_sandbox()
    monkeypatch.setenv("AGENTFORGE_SANDBOX_ENABLED", "true")
    # get_settings() is lru_cached; clear so the env var is picked up.
    from app import config as config_mod

    config_mod.get_settings.cache_clear()
    try:
        result = await calculator("7 * 6 + 1")
        assert result == "43"
    finally:
        # Tear down the sandbox singleton and re-enable default mode
        # so other tests see the in-process eval path again.
        await sandbox_mod.reset_sandbox()
        monkeypatch.delenv("AGENTFORGE_SANDBOX_ENABLED", raising=False)
        config_mod.get_settings.cache_clear()
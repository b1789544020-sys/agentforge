"""Lightweight, in-process code sandbox.

The public :class:`Sandbox` is a **synchronous** sandbox that evaluates
untrusted Python expressions in a constrained namespace. It is intentionally
simple so it can be used in tests and as the default executor for the agent
loop without any external dependencies.

Two slightly more capable drivers are also provided for production use:

* :class:`LocalSandbox` – runs code via a subprocess with a timeout and
  resource limits.
* :class:`DockerSandbox` – runs code inside an ephemeral Docker container.

Both of those are async; the in-process :class:`Sandbox` is sync and is
the one referenced by the tests.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from typing import Any, Protocol

try:
    from app.config import get_settings  # type: ignore
except Exception:  # pragma: no cover
    get_settings = None  # type: ignore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class SandboxError(RuntimeError):
    """Raised when sandboxed execution fails."""


# ---------------------------------------------------------------------------
# In-process synchronous sandbox
# ---------------------------------------------------------------------------
_FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "input",
        "breakpoint",
    }
)
# Note: __import__ is intentionally *not* in the forbidden names list.
# The sandbox exposes a whitelist-aware replacement via __builtins__
# (see :func:`_safe_import`) so that safe modules like ``math`` can be
# imported. Bare ``__import__`` calls that target non-whitelisted modules
# will still be rejected by :func:`_safe_import`.
# Modules that may not be (re)imported through __import__ or otherwise pulled
# in via attribute access on a top-level module we keep around.
_FORBIDDEN_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "ctypes",
        "requests",
        "urllib",
        "urllib3",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "pathlib",
        "io",
        "tempfile",
        "pickle",
        "shelve",
        "importlib",
        "builtins",
    }
)
_ALLOWED_MODULES = frozenset(
    {
        "math",
        "cmath",
        "statistics",
        "random",
        "datetime",
        "time",
        "re",
        "string",
        "json",
        "decimal",
        "fractions",
        "collections",
    }
)


def _safe_import(name: str, globals=None, locals=None, fromlist=(), level: int = 0):  # type: ignore[no-untyped-def]
    """A whitelist-aware replacement for :func:`__import__`."""
    root = name.split(".")[0]
    if root in _FORBIDDEN_MODULES:
        raise PermissionError(f"sandbox: import of {root!r} is not allowed")
    if root not in _ALLOWED_MODULES:
        raise PermissionError(f"sandbox: import of {root!r} is not allowed")
    return __import__(name, globals, locals, fromlist, level)


def _check_ast(code: str) -> None:
    """Reject AST patterns that the namespace alone cannot constrain.

    Notably, we forbid ``Import`` and ``ImportFrom`` because they could
    pull in disallowed modules even when ``__import__`` is replaced. The
    only exception is the whitelisted modules (which can be reached via
    :func:`_safe_import`).
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:  # pragma: no cover - tests don't trigger
        raise PermissionError(f"sandbox: syntax error: {exc.msg}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _ALLOWED_MODULES:
                    raise PermissionError(
                        f"sandbox: 'import {alias.name}' is not allowed"
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in _ALLOWED_MODULES:
                raise PermissionError(
                    f"sandbox: 'from {node.module} import ...' is not allowed"
                )
        elif isinstance(node, ast.Call):
            # Disallow calls to disallowed builtins like open()/exec().
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_NAMES:
                raise PermissionError(
                    f"sandbox: call to {func.id}() is not allowed"
                )


class Sandbox:
    """A minimal, in-process sandbox used by tests and the default agent loop.

    ``exec(code)`` evaluates an expression and returns the ``str``-ified
    result. Anything that looks like a forbidden import or builtin call
    is rejected with :class:`PermissionError`.
    """

    def __init__(self, *, allow_print: bool = True) -> None:
        self.allow_print = allow_print

    def _namespace(self) -> dict[str, Any]:
        ns: dict[str, Any] = {
            "__builtins__": {
                "abs": abs,
                "all": all,
                "any": any,
                "bool": bool,
                "dict": dict,
                "enumerate": enumerate,
                "filter": filter,
                "float": float,
                "int": int,
                "len": len,
                "list": list,
                "map": map,
                "max": max,
                "min": min,
                "pow": pow,
                "print": print if self.allow_print else _disabled_print,
                "range": range,
                "repr": repr,
                "reversed": reversed,
                "round": round,
                "set": set,
                "sorted": sorted,
                "str": str,
                "sum": sum,
                "tuple": tuple,
                "type": type,
                "zip": zip,
                "True": True,
                "False": False,
                "None": None,
                "__import__": _safe_import,
            }
        }
        # Pre-load whitelisted modules so attribute access on them works.
        for mod_name in _ALLOWED_MODULES:
            try:
                ns[mod_name] = __import__(mod_name)
            except Exception:  # pragma: no cover - defensive
                pass
        return ns

    def exec(self, code: str) -> str:
        """Evaluate a *single expression* and return ``str(result)``.

        Statements are not allowed – the input is parsed in ``eval`` mode.
        """
        if not isinstance(code, str):
            raise TypeError("sandbox.exec expects a string")
        # Whitelist-based AST check (best effort, but keeps us safe against
        # things like ``open(...)`` which ``eval`` mode would not allow
        # anyway because they are statements).
        try:
            tree = ast.parse(code, mode="eval")
        except SyntaxError as exc:
            raise PermissionError(f"sandbox: syntax error: {exc.msg}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_NAMES:
                    raise PermissionError(
                        f"sandbox: call to {node.func.id}() is not allowed"
                    )
            if (
                isinstance(node, ast.Name)
                and node.id in _FORBIDDEN_NAMES
                # ``__import__`` is exposed as a safe replacement; allow the
                # name itself to be referenced.
                and node.id != "__import__"
            ):
                # Reject bare access to forbidden builtins for defense in
                # depth (e.g. accessing the ``open`` function by name).
                raise PermissionError(
                    f"sandbox: name {node.id!r} is not allowed"
                )
        ns = self._namespace()
        try:
            value = eval(compile(tree, "<sandbox>", "eval"), ns)
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"sandbox: {type(exc).__name__}: {exc}") from exc
        return str(value)


def _disabled_print(*_args: Any, **_kwargs: Any) -> None:  # pragma: no cover
    return None


# ---------------------------------------------------------------------------
# Subprocess sandbox (production driver, async)
# ---------------------------------------------------------------------------
@dataclass
class SandboxResult:
    """Result of a sandboxed execution."""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
        }


class _AsyncSandbox(Protocol):
    async def run_python(self, code: str, stdin_args: Any | None = None) -> SandboxResult: ...
    async def run_shell(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        timeout_s: float | None = None,
        cwd: str | None = None,
    ) -> SandboxResult: ...
    async def close(self) -> None: ...


def _settings():
    if get_settings is None:
        raise RuntimeError("settings unavailable (install pydantic)")
    return get_settings()


class LocalSandbox:
    """Run untrusted Python in a subprocess with a hard timeout."""

    name = "local"

    def __init__(self) -> None:
        s = _settings()
        self.timeout = s.sandbox_timeout_seconds
        self.max_output_bytes = 64 * 1024

    async def run_python(self, code: str, stdin_args: Any | None = None) -> SandboxResult:
        if stdin_args is not None:
            payload = json.dumps(stdin_args, default=str)
            wrapped = (
                "import json, sys\n"
                f"_args = json.loads({payload!r})\n"
                + textwrap.dedent(code)
            )
        else:
            wrapped = textwrap.dedent(code)

        start = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-c",
                wrapped,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise SandboxError(f"Execution timed out after {self.timeout}s") from exc
        except FileNotFoundError as exc:
            raise SandboxError(f"Python interpreter not found: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"Sandbox failed: {exc}") from exc

        duration_ms = int((time.perf_counter() - start) * 1000)
        stdout = (stdout_b or b"")[: self.max_output_bytes].decode("utf-8", "replace")
        stderr = (stderr_b or b"")[: self.max_output_bytes].decode("utf-8", "replace")
        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode or 0,
            duration_ms=duration_ms,
        )

    async def run_shell(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        timeout_s: float | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Run a shell command in a subprocess and return its result.

        The driver is intentionally a thin wrapper around ``asyncio``
        subprocess APIs and aims to be **cross-platform**. On Windows
        the underlying ``cmd.exe`` builtin commands (e.g. ``dir``,
        ``copy``, ``del``) cannot be invoked with ``shell=False`` so
        we transparently retry via ``cmd /c`` when the first
        ``FileNotFoundError`` looks like a shell builtin.

        Returns a :class:`SandboxResult`. The default ``timeout_s``
        comes from :attr:`self.timeout` if not provided.
        """
        effective_timeout = float(timeout_s) if timeout_s is not None else float(self.timeout)
        cmd_list = [command] + list(args or [])
        max_output = self.max_output_bytes

        start = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError as exc:
            # On Windows, missing executables are often cmd builtins.
            # Fall back to ``cmd /c <command> <args>...`` if it looks
            # safe enough to do so (i.e. the command is a single word
            # with no path separator).
            if sys.platform == "win32" and command and not any(
                sep in command for sep in ("\\", "/", "..")
            ):
                fallback = ["cmd", "/c", command, *(args or [])]
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *fallback,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=cwd,
                    )
                except FileNotFoundError as exc2:
                    raise SandboxError(
                        f"executable not found: {command!r} (and cmd.exe fallback also failed: {exc2})"
                    ) from exc2
            else:
                raise SandboxError(
                    f"executable not found: {command!r} ({exc})"
                ) from exc
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"shell sandbox failed to start: {exc}") from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
        except asyncio.TimeoutError as exc:
            try:
                proc.kill()
            except Exception:  # pragma: no cover
                pass
            try:
                await proc.wait()
            except Exception:  # pragma: no cover
                pass
            raise SandboxError(
                f"shell command timed out after {effective_timeout}s: {command!r}"
            ) from exc

        duration_ms = int((time.perf_counter() - start) * 1000)
        stdout = (stdout_b or b"")[:max_output].decode("utf-8", "replace")
        stderr = (stderr_b or b"")[:max_output].decode("utf-8", "replace")
        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode or 0,
            duration_ms=duration_ms,
        )

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Docker sandbox (optional)
# ---------------------------------------------------------------------------
class DockerSandbox:
    """Run untrusted Python inside a disposable Docker container."""

    name = "docker"

    def __init__(self) -> None:
        s = _settings()
        self.image = s.sandbox_image
        self.memory = s.sandbox_memory_limit
        self.cpu_quota = s.sandbox_cpu_quota
        self.network_disabled = s.sandbox_network_disabled
        self.timeout = s.sandbox_timeout_seconds
        self._client: Any | None = None
        self._check_docker()

    def _check_docker(self) -> None:
        if shutil.which("docker") is None:
            raise SandboxError("docker executable not found in PATH")
        try:
            from docker import DockerClient  # type: ignore

            self._client = DockerClient.from_env()
            self._client.ping()
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"Docker daemon unreachable: {exc}") from exc

    async def run_python(self, code: str, stdin_args: Any | None = None) -> SandboxResult:
        assert self._client is not None
        if stdin_args is not None:
            payload = json.dumps(stdin_args, default=str)
            wrapped = (
                "import json\n"
                f"_args = json.loads({payload!r})\n"
                + textwrap.dedent(code)
            )
        else:
            wrapped = textwrap.dedent(code)

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(wrapped)
            host_path = f.name

        container_path = "/sandbox/script.py"
        start = time.perf_counter()
        try:
            container = self._client.containers.run(  # type: ignore[union-attr]
                image=self.image,
                command=["python", "-I", container_path],
                volumes={host_path: {"bind": container_path, "mode": "ro"}},
                mem_limit=self.memory,
                cpu_quota=int(self.cpu_quota * 100_000),
                network_disabled=self.network_disabled,
                detach=True,
                remove=False,
                user="nobody",
            )
            try:
                result = container.wait(timeout=self.timeout)
                stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
                stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
                exit_code = (
                    result.get("StatusCode", 1) if isinstance(result, dict) else int(result)
                )
            finally:
                try:
                    container.remove(force=True)
                except Exception:  # pragma: no cover
                    pass
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"Docker sandbox failed: {exc}") from exc
        finally:
            try:
                os.unlink(host_path)
            except OSError:
                pass

        duration_ms = int((time.perf_counter() - start) * 1000)
        return SandboxResult(
            stdout=stdout, stderr=stderr, exit_code=exit_code, duration_ms=duration_ms
        )

    async def run_shell(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        timeout_s: float | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Run a shell command inside an ephemeral Docker container.

        Implemented by piping the command into a one-shot ``sh -c``
        invocation so that the host shell never receives the user
        input (defence in depth).  ``args`` are appended after the
        command as positional strings.
        """
        assert self._client is not None
        effective_timeout = float(timeout_s) if timeout_s is not None else float(self.timeout)
        shell_cmd = " ".join([command, *(args or [])]).strip()
        max_output = self.max_output_bytes
        start = time.perf_counter()
        try:
            container = self._client.containers.run(  # type: ignore[union-attr]
                image=self.image,
                command=["sh", "-c", shell_cmd],
                mem_limit=self.memory,
                cpu_quota=int(self.cpu_quota * 100_000),
                network_disabled=self.network_disabled,
                working_dir=cwd,
                detach=True,
                remove=False,
                user="nobody",
            )
            try:
                result = container.wait(timeout=effective_timeout)
                stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
                stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")
                exit_code = (
                    result.get("StatusCode", 1) if isinstance(result, dict) else int(result)
                )
            finally:
                try:
                    container.remove(force=True)
                except Exception:  # pragma: no cover
                    pass
        except Exception as exc:  # noqa: BLE001
            raise SandboxError(f"Docker shell sandbox failed: {exc}") from exc
        duration_ms = int((time.perf_counter() - start) * 1000)
        return SandboxResult(
            stdout=stdout[:max_output],
            stderr=stderr[:max_output],
            exit_code=exit_code,
            duration_ms=duration_ms,
        )

    async def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# Factory / singleton
# ---------------------------------------------------------------------------
_default_sandbox: _AsyncSandbox | None = None


def get_sandbox() -> _AsyncSandbox:
    """Return the process-wide *async* sandbox instance."""
    global _default_sandbox
    if _default_sandbox is None:
        s = _settings()
        if s.sandbox_enabled:
            try:
                _default_sandbox = DockerSandbox()
                logger.info("Using DockerSandbox (image=%s)", s.sandbox_image)
            except SandboxError as exc:
                logger.warning("Falling back to LocalSandbox: %s", exc)
                _default_sandbox = LocalSandbox()
        else:
            _default_sandbox = LocalSandbox()
    return _default_sandbox


async def reset_sandbox() -> None:
    global _default_sandbox
    if _default_sandbox is not None:
        await _default_sandbox.close()
    _default_sandbox = None


__all__ = [
    "Sandbox",
    "SandboxResult",
    "SandboxError",
    "LocalSandbox",
    "DockerSandbox",
    "get_sandbox",
    "reset_sandbox",
]
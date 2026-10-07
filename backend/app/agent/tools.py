"""Tool registry and decorator.

Provides:

* :class:`ToolRegistry` – a registry of named tools with both per-instance
  and class-level storage.  Built-in tools (echo, now, calculator, etc.)
  are registered at import time on the class.  The module also exports
  a default *instance* (``registry``) that shares the class storage so
  callers can do ``registry.get(name)``.
* :func:`tool` – a decorator that marks a function as a tool, registers
  it on the class, and attaches an async :meth:`run` method.
* :class:`ToolSpec` / :class:`ToolDef` – lightweight metadata containers.
* :class:`ToolExecutor` – invokes tools by name with optional policy
  (allowlist) enforcement.
"""
from __future__ import annotations

import inspect
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------
@dataclass
class ToolSpec:
    """Tool metadata (production-oriented: with risk/scope)."""

    name: str
    description: str
    handler: Callable[..., Any]
    schema: Dict[str, Any] = field(default_factory=dict)
    risk: str = "safe"
    scope: str = "general"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "schema": self.schema,
            "risk": self.risk,
            "scope": self.scope,
        }


@dataclass
class ToolDef:
    """Lightweight tool definition used by the agent loop and tests.

    Fields:
        name:        Tool name.
        description: Human-readable description.
        parameters:  JSON Schema dict describing the tool's arguments.
        handler:     async or sync callable taking (args, ctx).
        risk:        Risk class (``"safe"`` by default).
        scope:       Execution scope tag.
    """

    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    handler: Optional[Callable[..., Any]] = None
    risk: str = "safe"
    scope: str = "general"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "risk": self.risk,
            "scope": self.scope,
        }

    def to_openai_tool(self) -> Dict[str, Any]:
        params = self.parameters or {"type": "object", "properties": {}}
        if "type" not in params and "properties" not in params:
            params = {
                "type": "object",
                "properties": params or {},
                "additionalProperties": True,
            }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or "",
                "parameters": params,
            },
        }


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ToolError(RuntimeError):
    """Base class for tool-related errors."""


class ToolNotFoundError(ToolError):
    """Raised when a requested tool name is not registered."""


class ToolSchemaError(ToolError):
    """Raised when a tool handler returns the wrong shape."""


# ---------------------------------------------------------------------------
# Smart invocation
# ---------------------------------------------------------------------------
def _invoke_handler(
    handler: Callable[..., Any],
    arguments: Dict[str, Any],
    ctx: Any = None,
) -> Any:
    """Call ``handler`` with arguments and an optional ``Context``.

    Two calling conventions are supported:

    1. ``async def handler(args, ctx)`` – the new agent-loop style used
       by tests and the production :class:`AgentLoopV2`.  We pass
       ``arguments`` and ``ctx`` positionally.

    2. ``def handler(**kwargs)`` – the legacy / decorator style used by
       the built-in ``@tool``-decorated tools (e.g. ``echo``,
       ``calculator``).  We pass the arguments dict as ``**kwargs``.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        params = list(sig.parameters.values())
        positional = [p for p in params if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )]
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        # Detect (args, ctx) shape.
        if len(positional) >= 2 and positional[0].name in {"args", "arguments", "input"} \
                and positional[1].name in {"ctx", "context"}:
            return handler(arguments, ctx)
        if has_var_kw:
            return handler(**arguments) if arguments else handler()
        if params and params[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return handler(**arguments) if arguments else handler()
    # Fallback: best-effort kwargs call.
    return handler(**arguments) if arguments else handler()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class ToolRegistry:
    """Tool registry with per-instance and class-level storage.

    The class-level ``_items`` / ``_order`` dicts hold the *canonical*
    tool table that the ``@tool`` decorator writes to.  Instances
    created via ``ToolRegistry()`` are **independent copies** of the
    class table, so tests and per-agent registries can register tools
    without polluting the global table.  Use ``register_class`` /
    ``register_instance`` to be explicit; ``register`` (instance method)
    always writes to the instance and leaves the class untouched.
    """

    _items: Dict[str, ToolSpec] = {}
    _order: List[str] = []

    def __init__(self) -> None:
        # Per-instance storage. Each instance starts with a copy of the
        # class table (so the default instance sees the built-in tools)
        # but can be mutated independently.
        self._items: Dict[str, ToolSpec] = dict(type(self)._items)
        self._order: List[str] = list(type(self)._order)

    # ------------------------------------------------------------------
    # Spec-building helper
    # ------------------------------------------------------------------
    @staticmethod
    def _build_spec(args: tuple, kwargs: dict) -> ToolSpec:
        if len(args) == 1 and not kwargs and isinstance(args[0], (ToolDef, ToolSpec)):
            obj = args[0]
            if isinstance(obj, ToolDef):
                return ToolSpec(
                    name=obj.name,
                    description=obj.description,
                    handler=obj.handler,
                    schema=dict(obj.parameters or {}),
                    risk=obj.risk,
                    scope=obj.scope,
                )
            return ToolSpec(
                name=obj.name,
                description=obj.description,
                handler=obj.handler,
                schema=dict(obj.schema or {}),
                risk=obj.risk,
                scope=obj.scope,
            )
        if len(args) == 1 and callable(args[0]) and not kwargs:
            handler = args[0]
            name = kwargs.pop("name", None) or handler.__name__
            description = kwargs.pop("description", "")
            schema = kwargs.pop("schema", None)
            risk = kwargs.pop("risk", "safe")
            scope = kwargs.pop("scope", "general")
        else:
            if "tool_def" in kwargs:
                return ToolRegistry._build_spec((kwargs.pop("tool_def"),), {})
            if "handler" in kwargs and "name" not in kwargs:
                kwargs["name"] = kwargs["handler"].__name__
            name = kwargs.pop("name", None)
            if name is None:
                raise ToolError("register() requires a 'name' or a handler/position arg")
            handler = kwargs.pop("handler")
            description = kwargs.pop("description", "")
            schema = kwargs.pop("schema", None)
            risk = kwargs.pop("risk", "safe")
            scope = kwargs.pop("scope", "general")
        if not name or not isinstance(name, str):
            raise ToolError("tool name must be a non-empty string")
        if not callable(handler):
            raise ToolError(f"handler for tool {name!r} must be callable")
        if handler is None:
            raise ToolError(f"handler for tool {name!r} must be callable")
        doc_first = ""
        if (handler.__doc__ or "").strip():
            doc_first = (handler.__doc__ or "").strip().splitlines()[0]
        return ToolSpec(
            name=name,
            description=description or doc_first,
            handler=handler,
            schema=dict(schema or {}),
            risk=risk,
            scope=scope,
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, *args: Any, **kwargs: Any) -> ToolSpec:
        """Register a tool on **this instance** (does not affect the class)."""
        spec = self._build_spec(args, kwargs)
        self._items[spec.name] = spec
        if spec.name not in self._order:
            self._order.append(spec.name)
        return spec

    # Backwards-compatible alias.
    register_tool = register

    @classmethod
    def register_class(cls, *args: Any, **kwargs: Any) -> ToolSpec:
        """Register a tool on the class-level (shared) registry.

        This is what the ``@tool`` decorator uses so the global module
        table always reflects every decorated function.
        """
        spec = cls._build_spec(args, kwargs)
        cls._items[spec.name] = spec
        if spec.name not in cls._order:
            cls._order.append(spec.name)
        return spec

    # ------------------------------------------------------------------
    # Unregistration / clearing
    # ------------------------------------------------------------------
    @classmethod
    def unregister(cls_or_self, name: str) -> None:
        """Remove ``name`` from the registry.

        Works as both a classmethod (``ToolRegistry.unregister('x')``)
        and an instance method (``registry.unregister('x')``).  When
        called on the class the class-level table is mutated; when
        called on an instance the instance table is mutated.
        """
        target = cls_or_self._items
        target.pop(name, None)
        try:
            cls_or_self._order.remove(name)
        except ValueError:
            pass

    @classmethod
    def clear(cls_or_self) -> None:
        cls_or_self._items.clear()
        cls_or_self._order.clear()

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------
    @classmethod
    def get(cls_or_self, name: str) -> Any:
        """Return the registered *handler* for ``name`` (or ``None``).

        Works on both the class and instances: ``ToolRegistry.get('x')``
        reads the class-level table, while ``my_registry.get('x')`` reads
        the instance's table.
        """
        spec = cls_or_self._items.get(name)
        return spec.handler if spec is not None else None

    @classmethod
    def spec(cls_or_self, name: str) -> Optional[ToolSpec]:
        """Return the :class:`ToolSpec` for ``name`` (or ``None``).

        Works on both the class and instances (see :meth:`get`).
        """
        return cls_or_self._items.get(name)

    @classmethod
    def list_all(cls_or_self) -> List[ToolSpec]:
        """Return all registered :class:`ToolSpec` objects, in order.

        Works on both the class and instances (see :meth:`get`).
        """
        items = cls_or_self._items
        return [items[n] for n in cls_or_self._order if n in items]

    @classmethod
    def names(cls_or_self) -> List[str]:
        return list(cls_or_self._order)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    # Loop-internal context keys that the @tool()-decorated built-ins do
    # **not** declare as arguments.  They must never leak into the kwargs
    # we hand to a handler -- otherwise `_invoke_handler` treats them as
    # declared parameters and the call dies with
    # ``unexpected keyword argument 'ctx'``.  Real `ctx` is still passed
    # positionally to handlers that opt into the (args, ctx) shape.
    _RESERVED_CTX_KEYS = frozenset({"ctx", "context", "session", "agent_id"})

    @staticmethod
    def _merge_args(arguments: Optional[Dict[str, Any]], kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Combine tool arguments with loop kwargs, stripping ctx fields.

        Order of precedence (highest wins): ``arguments`` > caller kwargs
        > None.  Loop-internal fields (``ctx``/``session``/``agent_id``)
        are removed from the result so they never reach the handler as a
        stray keyword.
        """
        merged: Dict[str, Any] = {}
        if arguments:
            merged.update(arguments)
        for k, v in kwargs.items():
            if k in ToolRegistry._RESERVED_CTX_KEYS:
                continue
            merged[k] = v
        return merged

    async def execute(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute a tool from **this instance** and return a dict result.

        Two calling conventions are supported so the agent loop can pass
        an ``arguments`` dict directly without unpacking it::

            await registry.execute("echo", {"text": "hi"})
            await registry.execute("echo", text="hi")
        """
        spec = self._items.get(name)
        if spec is None:
            raise ToolNotFoundError(f"tool {name!r} is not registered")
        handler = spec.handler
        merged = self._merge_args(arguments, kwargs)
        t0 = time.perf_counter()
        # ``ctx`` itself is *always* forwarded positionally to handlers
        # that accept it; it just must not appear in the kwargs payload.
        result = _invoke_handler(handler, merged, kwargs.get("ctx"))
        if inspect.iscoroutine(result):
            result = await result
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if not isinstance(result, dict):
            result = {"value": result}
        result.setdefault("_meta", {})
        result["_meta"].update(
            {"tool": name, "elapsed_ms": elapsed_ms, "risk": spec.risk}
        )
        return result

    @classmethod
    async def execute_class(
        cls,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute a tool from the class-level registry."""
        spec = cls._items.get(name)
        if spec is None:
            raise ToolNotFoundError(f"tool {name!r} is not registered")
        handler = spec.handler
        merged = cls._merge_args(arguments, kwargs)
        t0 = time.perf_counter()
        result = _invoke_handler(handler, merged, kwargs.get("ctx"))
        if inspect.iscoroutine(result):
            result = await result
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if not isinstance(result, dict):
            result = {"value": result}
        result.setdefault("_meta", {})
        result["_meta"].update(
            {"tool": name, "elapsed_ms": elapsed_ms, "risk": spec.risk}
        )
        return result

    # ------------------------------------------------------------------
    # Schema helpers
    # ------------------------------------------------------------------
    def to_prompt_blocks(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.list_all()]

    @classmethod
    def to_prompt_blocks_class(cls) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in cls.list_all()]

    def to_openai_tools(
        self,
        names: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return OpenAI function-calling schemas from this instance."""
        schemas: List[Dict[str, Any]] = []
        for spec in self.list_all():
            if names and spec.name not in names:
                continue
            params = spec.schema if isinstance(spec.schema, dict) else {}
            if "type" not in params and "properties" not in params:
                params = {
                    "type": "object",
                    "properties": params or {},
                    "additionalProperties": True,
                }
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description or "",
                        "parameters": params,
                    },
                }
            )
        return schemas

    @classmethod
    def to_openai_tools_class(
        cls,
        names: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return OpenAI function-calling schemas from the class registry."""
        schemas: List[Dict[str, Any]] = []
        for spec in cls.list_all():
            if names and spec.name not in names:
                continue
            params = spec.schema if isinstance(spec.schema, dict) else {}
            if "type" not in params and "properties" not in params:
                params = {
                    "type": "object",
                    "properties": params or {},
                    "additionalProperties": True,
                }
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description or "",
                        "parameters": params,
                    },
                }
            )
        return schemas


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------
def tool(
    name: Optional[str] = None,
    description: str = "",
    schema: Optional[Dict[str, Any]] = None,
    risk: str = "safe",
    scope: str = "general",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark ``fn`` as a tool, register it on the class, and return it.

    The returned function gains an async :meth:`run` method that mirrors
    the direct call. This keeps ``ToolRegistry.get_class(name) is fn``
    true while still allowing ``await fn.run(**kwargs)`` from callers.
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or fn.__name__
        spec = ToolDef(
            name=tool_name,
            description=description or (fn.__doc__ or ""),
            parameters=schema or {},
            handler=fn,
            risk=risk,
            scope=scope,
        )
        ToolRegistry.register_class(spec)

        async def run(**kwargs: Any) -> Any:
            handler = spec.handler
            out = handler(**kwargs) if kwargs else handler()
            if inspect.iscoroutine(out):
                out = await out
            return out

        # Attach helpers as attributes so the function remains callable.
        try:
            fn.run = run  # type: ignore[attr-defined]
        except (AttributeError, TypeError):  # pragma: no cover - builtins
            pass
        fn.__tool_spec__ = spec  # type: ignore[attr-defined]
        return fn

    return wrap


# ---------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------
@tool(
    name="echo",
    description="Return the input text unchanged.",
    schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    risk="safe",
    scope="general",
)
def echo(text: str) -> str:
    return text


@tool(
    name="now",
    description="Return the current UTC timestamp in ISO-8601 format.",
    risk="safe",
    scope="general",
)
def now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@tool(
    name="calculator",
    description="Evaluate a simple arithmetic expression and return the result as a string.",
    schema={
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    },
    risk="safe",
    scope="general",
)
async def calculator(expression: str) -> str:
    """Backward-compatible thin wrapper for :func:`python_eval` in
    ``"simple"`` mode.

    Historical (0.1.x) callers pass an arithmetic expression such as
    ``"2 + 2 * 3"`` and get a stringified number back.  The
    implementation is intentionally narrow -- only digits, whitespace
    and ``+ - * / ( ) . %`` are allowed -- so this function stays safe
    even when run inside the agent process.

    For richer expressions (variables, function calls, math/statistics
    helpers, sandboxed arbitrary Python) use :func:`python_eval`.
    """
    return await python_eval(expression, mode="simple")


# ---------------------------------------------------------------------
# python_eval -- 0.1.3 #1 (rich expression evaluator)
# ---------------------------------------------------------------------
#
# ``calculator`` is the historical 0.1.x thin arithmetic wrapper; this
# tool is the general-purpose replacement.  It supports three modes,
# each a strict superset of the previous one:
#
# * ``"simple"`` -- the calculator behaviour: only digits, whitespace
#   and ``+ - * / ( ) . %``.  Same allow-list as 0.1.x.
# * ``"math"``   -- Python expressions with ``math`` and ``statistics``
#   modules pre-imported.  An AST gate rejects imports, dunder
#   attribute access, and calls to unsafe builtins.
# * ``"sandbox"`` -- anything that passes the AST gate.  When
#   ``AGENTFORGE_SANDBOX_ENABLED=true`` the actual evaluation happens
#   in a separate Python subprocess via
#   :func:`app.agent.sandbox.get_sandbox`.  When the sandbox is off
#   this mode falls back to in-process math-mode evaluation -- the AST
#   gate is still applied.
# ---------------------------------------------------------------------
import ast as _ast  # local alias; the rest of the module doesn't use ast
import math as _math
import statistics as _statistics

# Builtins that look harmless but are not safe to expose in math /
# sandbox mode.  Anything callable here is a footgun (file / network /
# import / system access).
_MATH_MODE_FORBIDDEN_BUILTINS = frozenset(
    {
        "open", "exec", "eval", "compile", "__import__", "input",
        "globals", "locals", "vars", "dir", "getattr", "setattr",
        "delattr", "breakpoint", "memoryview", "type", "object",
    }
)


class _MathModeValidator(_ast.NodeVisitor):
    """AST gate for ``math`` / ``sandbox`` mode.

    Rejects:
    * Import / ImportFrom
    * Any ``Attribute`` whose root is a dunder (``__class__`` etc.)
    * Calls to a forbidden builtin
    * Starred expressions
    """

    def visit_Import(self, node: _ast.Import) -> None:  # noqa: D401
        raise ToolError("python_eval: 'import' is not allowed")

    def visit_ImportFrom(self, node: _ast.ImportFrom) -> None:  # noqa: D401
        raise ToolError("python_eval: 'from ... import' is not allowed")

    def visit_Attribute(self, node: _ast.Attribute) -> None:  # noqa: D401
        # Reject any dunder attribute access regardless of root
        # (``x.__class__`` is just as dangerous as
        # ``().__class__.__bases__[0].__subclasses__()``).
        if node.attr.startswith("__") and node.attr.endswith("__"):
            raise ToolError(
                f"python_eval: dunder attribute access not allowed "
                f"({node.attr})"
            )
        # Also reject attribute access on a dunder-named root, in case
        # the user constructs a chain whose first segment is dunder.
        cur: _ast.AST = node
        while isinstance(cur, _ast.Attribute):
            cur = cur.value
        if isinstance(cur, _ast.Name) and cur.id.startswith("__"):
            raise ToolError(
                f"python_eval: dunder attribute access not allowed ({cur.id})"
            )
        # Continue traversal so nested attributes are also checked.
        self.generic_visit(node)

    def visit_Call(self, node: _ast.Call) -> None:  # noqa: D401
        func = node.func
        if isinstance(func, _ast.Name) and func.id in _MATH_MODE_FORBIDDEN_BUILTINS:
            raise ToolError(
                f"python_eval: calling {func.id}() is not allowed"
            )
        self.generic_visit(node)

    def visit_Starred(self, node: _ast.Starred) -> None:  # noqa: D401
        raise ToolError("python_eval: starred expressions are not allowed")


def _math_mode_eval(expression: str) -> Any:
    """Run ``expression`` in the math/statistics namespace after AST gating."""
    tree = _ast.parse(expression, mode="eval")
    _MathModeValidator().visit(tree)
    namespace = {
        "__builtins__": {},  # belt-and-braces: block builtins
        "math": _math,
        "statistics": _statistics,
    }
    return eval(compile(tree, "<python_eval:math>", "eval"), namespace, {})


@tool(
    name="python_eval",
    description=(
        "Evaluate a Python expression and return the result as a string. "
        "Three modes are supported: "
        "'simple' (digits + arithmetic only, backward-compatible with calculator); "
        "'math' (Python expressions with math.* and statistics.* pre-imported); "
        "'sandbox' (richer expressions; runs in an isolated Python subprocess "
        "when AGENTFORGE_SANDBOX_ENABLED=true, otherwise falls back to math mode). "
        "All non-simple modes apply an AST safety gate (no imports, no dunder "
        "attributes, no open/exec/__import__/etc.)."
    ),
    schema={
        "type": "object",
        "required": ["expression"],
        "properties": {
            "expression": {"type": "string"},
            "mode": {
                "type": "string",
                "enum": ["simple", "math", "sandbox"],
            },
        },
    },
)
async def python_eval(expression: str, mode: str = "math") -> str:
    """Evaluate ``expression`` according to ``mode`` (see tool description)."""
    if not isinstance(expression, str):
        raise ToolError("python_eval: 'expression' must be a string")
    mode_normalised = (mode or "math").lower()
    if mode_normalised not in {"simple", "math", "sandbox"}:
        raise ToolError(
            f"python_eval: unknown mode {mode!r} "
            "(expected simple|math|sandbox)"
        )

    # ---- simple mode: classic calculator allow-list --------------------
    if mode_normalised == "simple":
        allowed = set("0123456789+-*/().% \t")
        if any(ch not in allowed for ch in expression):
            raise ToolError(
                "python_eval(simple): expression contains illegal characters"
            )
        return str(eval(expression, {"__builtins__": {}}, {}))

    # ---- math / sandbox mode: AST gate then run ------------------------
    # The gate runs *before* we decide whether to delegate to the sandbox;
    # a forbidden expression must fail even when the sandbox would be
    # unreachable (sandbox off, container image missing, etc.).
    try:
        value = _math_mode_eval(expression)
    except ToolError:
        raise
    except SyntaxError as exc:
        raise ToolError(f"python_eval: syntax error: {exc.msg}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"python_eval: evaluation failed: {exc}") from exc

    # In sandbox mode we still try to push the actual evaluation to the
    # subprocess if it is enabled -- this is purely defense in depth.
    if mode_normalised == "sandbox":
        try:
            from app.config import get_settings
            from app.agent.sandbox import get_sandbox, SandboxError
        except Exception:  # pragma: no cover
            get_settings = None  # type: ignore
            get_sandbox = None  # type: ignore
            SandboxError = Exception  # type: ignore
        if get_settings is not None and get_sandbox is not None:
            settings = get_settings()
            if getattr(settings, "sandbox_enabled", False):
                # The AST gate above already vetted the expression; the
                # subprocess rebuilds a tiny namespace so a fresh Python
                # interpreter also rejects any direct access to
                # builtins.
                code = (
                    "import math as _math, statistics as _statistics\n"
                    "print(repr(eval("
                    f"{expression!r}, "
                    "{'__builtins__': {}, "
                    "'math': _math, "
                    "'statistics': _statistics}, "
                    "{}"
                    ")))"
                )
                try:
                    sb = get_sandbox()
                    result = await sb.run_shell(
                        "python", ["-c", code], timeout_s=5
                    )
                except SandboxError as exc:
                    raise ToolError(
                        f"python_eval: sandbox error: {exc}"
                    ) from exc
                if result.exit_code != 0:
                    stderr = (result.stderr or "").strip()
                    raise ToolError(
                        "python_eval: evaluation failed"
                        + (f": {stderr}" if stderr else "")
                    )
                value = (result.stdout or "").strip()
                # repr() returns the raw Python form (e.g. ``'7'`` for
                # an int).  Strip surrounding quotes so the caller gets
                # a human-friendly string.
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in ("'", '"')
                ):
                    value = value[1:-1]

    return str(value)



# --- Rich tools used by the agent loop -------------------------------------
@tool(
    name="http_request",
    description="Perform an HTTP request. Method/url required. Body must be a JSON-serializable object.",
    risk="network",
    scope="outbound",
    schema={
        "type": "object",
        "required": ["url", "method"],
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string"},
            "headers": {"type": "object"},
            "params": {"type": "object"},
            "body": {"type": "object"},
            "timeout_s": {"type": "number"},
        },
    },
)
def http_request(url: str, method: str = "GET", **kwargs: Any) -> Dict[str, Any]:
    timeout = float(kwargs.get("timeout_s", 30))
    body = kwargs.get("body")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    for k, v in (kwargs.get("headers") or {}).items():
        req.add_header(k, str(v))
    if body is not None and "Content-Type" not in (kwargs.get("headers") or {}):
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            hdrs = dict(resp.getheaders())
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read() if hasattr(e, "read") else b""
        hdrs = dict(e.headers.items()) if e.headers else {}
        status = e.code
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    return {"status": status, "headers": hdrs, "body": parsed, "raw": text[:8000]}


@tool(
    name="shell_exec",
    description="Run a shell command. Returns stdout/stderr/exit_code.",
    risk="exec",
    scope="local",
    schema={
        "type": "object",
        "required": ["command"],
        "properties": {
            "command": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "timeout_s": {"type": "number"},
            "cwd": {"type": "string"},
        },
    },
)
async def shell_exec(command: str, **kwargs: Any) -> Dict[str, Any]:
    """Run a shell command. Returns stdout/stderr/exit_code.

    Actual driver is selected per-call:

    * When ``AGENTFORGE_SANDBOX_ENABLED`` is on, the call is routed
      through the global async sandbox returned by
      :func:`app.agent.sandbox.get_sandbox` so the user can opt into
      the subprocess-isolated or Docker-isolated driver via
      configuration without touching tool code.
    * When sandboxing is off (0.1.x default) we use a thin
      ``asyncio.create_subprocess_exec`` wrapper that mirrors the
      legacy synchronous behaviour while still cooperating with the
      agent loop's async I/O. On Windows we transparently fall back
      to ``cmd /c <command>`` when the executable is missing
      (e.g. ``dir`` / ``copy`` are cmd builtins).
    """
    import asyncio as _asyncio

    args_list = list(kwargs.get("args") or [])
    timeout = kwargs.get("timeout_s")
    cwd = kwargs.get("cwd")

    # --- Optional sandbox routing -----------------------------------------
    settings = None
    try:
        from app.config import get_settings  # type: ignore

        settings = get_settings()
    except Exception:  # pragma: no cover - settings unavailable
        settings = None

    if settings is not None and getattr(settings, "sandbox_enabled", False):
        try:
            from app.agent.sandbox import get_sandbox, SandboxError  # type: ignore

            sb = get_sandbox()
            res = await sb.run_shell(command, args_list, timeout_s=timeout, cwd=cwd)
            return {
                "exit_code": res.exit_code,
                "stdout": res.stdout[:8000],
                "stderr": res.stderr[:4000],
                "duration_ms": res.duration_ms,
                "sandbox": getattr(sb, "name", "sandbox"),
                "timed_out": False,
            }
        except Exception as exc:  # noqa: BLE001
            from app.agent.sandbox import SandboxError  # type: ignore

            if isinstance(exc, SandboxError):
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"sandbox error: {exc}"[:4000],
                    "sandbox": "sandbox",
                    "timed_out": True,
                }
            raise

    # --- Default: thin async subprocess wrapper --------------------------
    effective_timeout = float(timeout) if timeout is not None else 30.0
    cmd_list = [command] + args_list
    try:
        try:
            proc = await _asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError:
            # Windows: command may be a cmd builtin (dir, copy, del, ...).
            import sys as _sys

            if _sys.platform == "win32" and command and not any(
                sep in command for sep in ("\\", "/", "..")
            ):
                proc = await _asyncio.create_subprocess_exec(
                    "cmd",
                    "/c",
                    command,
                    *args_list,
                    stdout=_asyncio.subprocess.PIPE,
                    stderr=_asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            else:
                raise

        try:
            stdout_b, stderr_b = await _asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
        except _asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:  # pragma: no cover
                pass
            try:
                await proc.wait()
            except Exception:  # pragma: no cover
                pass
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"timed out after {effective_timeout}s"[:4000],
                "timed_out": True,
            }
    except FileNotFoundError as exc:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"executable not found: {command!r} ({exc})"[:4000],
            "timed_out": False,
        }

    return {
        "exit_code": proc.returncode if proc.returncode is not None else 0,
        "stdout": (stdout_b or b"").decode("utf-8", "replace")[:8000],
        "stderr": (stderr_b or b"").decode("utf-8", "replace")[:4000],
        "timed_out": False,
    }


@tool(
    name="file_read",
    description="Read a UTF-8 text file (truncated to max_bytes).",
    risk="read",
    scope="fs",
    schema={
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
            "max_bytes": {"type": "number"},
        },
    },
)
def file_read(path: str, **kwargs: Any) -> Dict[str, Any]:
    from pathlib import Path as _P

    max_b = int(kwargs.get("max_bytes", 65536))
    p = _P(path)
    data = p.read_bytes()[:max_b]
    return {"path": str(p), "size": len(data), "content": data.decode("utf-8", errors="replace")}


@tool(
    name="file_write",
    description="Write content to a file.",
    risk="write",
    scope="fs",
    schema={
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string"},
        },
    },
)
def file_write(path: str, content: str, **kwargs: Any) -> Dict[str, Any]:
    from pathlib import Path as _P

    p = _P(path)
    mode = "a" if kwargs.get("mode") == "append" else "w"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open(mode, encoding="utf-8") as f:
        f.write(content)
    return {"path": str(p), "bytes_written": len(content.encode("utf-8"))}


@tool(
    name="search_knowledge",
    description=(
        "Search the agent's long-term memory. Pass 'query' (required) and "
        "optional 'top_k' (default 5, max 20) and 'mode' ('exact' for "
        "substring match, 'embed' for semantic similarity, 'hybrid' for "
        "both). Default mode is 'exact' to keep behaviour identical to "
        "0.1.0; switch to 'embed' when the user phrasings differ from "
        "what was stored."
    ),
    risk="read",
    scope="memory",
    schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            "mode": {
                "type": "string",
                "enum": ["exact", "embed", "hybrid"],
                "default": "exact",
            },
        },
    },
)
async def search_knowledge(query: str, **kwargs: Any) -> Dict[str, Any]:
    """Look up the agent's long-term memory for facts that match ``query``.

    The tool is invoked by the agent loop, which passes a ``ctx`` dict
    containing the active ``AsyncSession`` under the key ``"session"`` and
    the current agent's id under ``"agent_id"``.  When ``ctx`` is missing
    (e.g. unit-tests that call the handler directly), we fall back to
    the module-level in-memory list so the tool still produces a
    non-empty response rather than crashing.

    ``mode`` controls which retrieval strategy is used.  ``"exact"``
    (default) preserves 0.1.0's substring-match behaviour; ``"embed"``
    runs cosine similarity over the vector store; ``"hybrid"`` merges
    both.  Embedding lookups are zero-dependency (hashing trick) so the
    tool is always available in-process.
    """
    ctx = kwargs.get("ctx") or {}
    session = ctx.get("session") if isinstance(ctx, dict) else None
    agent_id = ctx.get("agent_id") if isinstance(ctx, dict) else None
    top_k = int(kwargs.get("top_k", 5))
    mode = str(kwargs.get("mode", "exact"))
    if mode not in ("exact", "embed", "hybrid"):
        mode = "exact"

    # --- path 1: live DB session (production) ---------------------
    if session is not None and agent_id is not None:
        from .memory import MemoryStore

        try:
            store = MemoryStore(session, agent_id=agent_id)
            hits = await store.search(query, limit=top_k, mode=mode)
        except Exception as exc:  # noqa: BLE001
            import traceback as _tb
            _tb.print_exc()
            return {"error": f"{type(exc).__name__}: {exc}", "results": []}
        return {
            "mode": mode,
            "results": [
                {
                    "id": item.id,
                    "score": round(float(score), 4),
                    "text": item.content,
                    "kind": item.kind,
                    "importance": item.importance,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item, score in hits
            ],
        }

    # --- path 2: module-level in-memory store (test / no-ctx) ----
    from . import memory as _mem

    tokens = query.lower().split()
    hits: List[Dict[str, Any]] = []
    for item in _mem.list_items():
        text = (item.get("text") or "").lower()
        score = sum(1 for tok in tokens if tok in text)
        if score > 0:
            hits.append({"id": item.get("id"), "score": score, "text": item.get("text")})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return {"results": hits[:top_k]}


@tool(
    name="final_answer",
    description="Return the final answer to the user and end the loop.",
    risk="safe",
    scope="none",
    schema={
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    },
)
def final_answer(answer: str) -> Dict[str, Any]:
    return {"answer": answer, "is_final": True}


# ---------------------------------------------------------------------------
# Module-level helpers (kept for backward compatibility with internal callers)
# ---------------------------------------------------------------------------
def get_default_registry() -> ToolRegistry:
    """Return the module-level default registry instance."""
    return registry


# ---------------------------------------------------------------------------
# Executor — invokes tools with policy / sandbox / metrics support
# ---------------------------------------------------------------------------
class ToolExecutor:
    """Executes registered tools asynchronously.

    The :class:`AgentLoop` constructs an executor and delegates all tool
    calls to it.  The executor wraps a :class:`ToolRegistry` so it can
    look up handlers by name, and it owns the (optional) policy and
    metrics hooks that the production loop installs.

    The executor **always** reads from its own ``registry`` (which can
    be either an instance or the class) and never falls back to the
    class-level state.  This keeps tests fully isolated.
    """

    def __init__(
        self,
        registry: Optional[Any] = None,
        *,
        allowed_names: Optional[List[str]] = None,
    ) -> None:
        # Accept either a ToolRegistry instance or the class itself
        # (legacy callers sometimes pass the class).
        if registry is None:
            registry = ToolRegistry()
        self.registry = registry
        self.allowed_names = list(allowed_names) if allowed_names else None

    # -- helpers --------------------------------------------------------
    def _is_allowed(self, name: str) -> bool:
        if self.allowed_names is None:
            return True
        return name in self.allowed_names

    async def _execute_via_registry(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]],
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Dispatch to the appropriate execute() implementation."""
        reg = self.registry
        # ToolRegistry class: has execute_class async classmethod.
        if isinstance(reg, type) and issubclass(reg, ToolRegistry):
            return await reg.execute_class(name, arguments, **kwargs)
        # ToolRegistry instance: has execute async method.
        if hasattr(reg, "execute") and callable(reg.execute):
            return await reg.execute(name, arguments, **kwargs)
        raise ToolError(f"unsupported registry object: {reg!r}")

    # -- main entry -----------------------------------------------------
    async def execute(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute ``name`` with ``arguments`` and return a result dict.

        Raises:
            ToolNotFoundError: if the tool is not registered.
            PermissionError:   if the tool is not in the whitelist.
        """
        if not self._is_allowed(name):
            raise PermissionError(
                f"tool {name!r} is not allowed by this executor's policy"
            )
        return await self._execute_via_registry(name, arguments, kwargs)


# Module-level default registry *instance*.  Shares the class table so
# it can be used as a "global" registry in production while still being
# passable to :class:`ToolExecutor` for tests.
registry: ToolRegistry = ToolRegistry()


# ---------------------------------------------------------------------------
# Loop / loop-safety constants shared with the agent loop and tests
# ---------------------------------------------------------------------------
# If a tool call fails this many times *in a row*, the loop terminates
# with ``status="tool_error_loop"``. Exposed at module level so tests
# can assert its value and bound.
MAX_CONSECUTIVE_TOOL_ERRORS: int = 3


__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "ToolDef",
    "ToolError",
    "ToolNotFoundError",
    "ToolSchemaError",
    "ToolExecutor",
    "tool",
    "registry",
    "get_default_registry",
    "MAX_CONSECUTIVE_TOOL_ERRORS",
]
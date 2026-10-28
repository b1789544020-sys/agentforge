import axios from "axios";

// Resolve the backend base URL.
//
// Precedence:
//   1. `VITE_API_BASE` env var (set in docker-compose.yml or a .env).
//   2. Relative `/api` (works when the dev-server proxy is in play).
//
// We then normalise the value so that `API_BASE + "/agents"` always
// resolves to a single canonical path.  Two foot-guns we explicitly
// guard against:
//   • Trailing slash: `"/api/"` → `/api/agents` (double slash) — we
//     strip trailing slashes.
//   • Missing `/api` prefix: `"http://host:8000"` → `/agents` (404,
//     because the FastAPI app exposes every route under `/api` or
//     `/api/v1`).  We append `/api` if the value is an absolute URL
//     that doesn't end in `/api` or `/api/v1`.
const RAW_BASE = (import.meta.env.VITE_API_BASE ?? "/api").trim();
function normaliseApiBase(raw: string): string {
  let base = raw.replace(/\/+$/, "");
  // Absolute URL (e.g. http://host:8000) — make sure it ends in /api.
  if (/^https?:\/\//i.test(base)) {
    if (!/\/api(?:\/v\d+)?$/i.test(base)) {
      base = `${base}/api`;
    }
  } else if (base === "" || base === "/") {
    base = "/api";
  } else if (!base.startsWith("/api") && !base.startsWith("api")) {
    base = `/api${base.startsWith("/") ? "" : "/"}${base}`;
  }
  return base;
}
export const API_BASE = normaliseApiBase(RAW_BASE);

export const api = axios.create({
  baseURL: API_BASE,
  timeout: 60_000,
});

/**
 * Build the WebSocket URL for a run's live trace stream.
 *
 * 0.1.2 #10 — the backend serves the stream at
 * ``/agents/{id}/runs/{id}/ws`` and FastAPI's WebSocket route is
 * mounted under both the versioned and the unversioned API prefix,
 * so we just take whatever ``API_BASE`` resolves to and swap the
 * scheme to ``ws`` / ``wss`` (matching the page's protocol).
 *
 * Examples (assuming API_BASE = "/api"):
 *   streamUrl("/api")                -> "ws(s)://host/api/agents/x/runs/y/ws"
 *   streamUrl("http://h:8000/api")   -> "ws://h:8000/api/agents/x/runs/y/ws"
 */
export function streamUrl(agentId: string, runId: string): string {
  const path = `${API_BASE}/agents/${agentId}/runs/${runId}/ws`;
  // Relative path (vite dev-server proxy) -- let the browser pick
  // ws/wss based on the current document location.
  if (path.startsWith("/")) {
    const proto = typeof window !== "undefined" && window.location.protocol === "https:"
      ? "wss:"
      : "ws:";
    return `${proto}//${window.location.host}${path}`;
  }
  // Absolute URL -- swap http(s) for ws(s).
  return path.replace(/^http/i, "ws");
}

// --- Types -----------------------------------------------------------------

export interface Agent {
  id: string;
  name: string;
  description?: string;
  system_prompt: string;
  tools: string[];
  model: string;
  created_at: string;
}

export interface ToolSpec {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

/**
 * One step in a Run's execution trace.
 *
 * Field names mirror what `app.api.runs._serialize_step` actually sends
 * over the wire (NOT what the old frontend expected).  See backend
 * `models.Step` for the underlying table schema.
 */
export interface Step {
  id: string;
  /** 1-based ordinal, assigned by the agent loop. */
  index: number;
  /**
   * What kind of step this is.  Matches `STEP_KIND_*` constants in
   * `app.models` (think / tool_call / tool_result / final_answer /
   * observation / error).
   */
  kind: "think" | "tool_call" | "tool_result" | "final_answer" | "observation" | "error";
  /** Tool name for `tool_call` / `tool_result`; null otherwise. */
  name?: string | null;
  /** Human-readable text body of the step (LLM content / tool output). */
  content?: string;
  /** Free-form structured data.  For tool_call: `{arguments, call_id}`.
   *  For tool_result: `{call_id, result}`.  For final_answer:
   *  `{prompt_tokens, completion_tokens}`. */
  payload?: Record<string, unknown>;
  /** Wall-clock duration of this step in ms. */
  duration_ms?: number | null;
  /** Token count attributed to this step (LLM completion tokens). */
  tokens?: number | null;
  /** "ok" | "error". */
  status?: string;
  created_at: string;
  /**
   * 0.1.3 #9 — tool risk class for ``tool_call`` steps.  The backend
   * (``app.api.runs._serialize_step`` for the REST path and
   * ``app.agent.loop._step_to_dict`` for the WebSocket path) reads
   * this off ``payload["risk"]`` and surfaces it as a top-level
   * field so the trace UI can render a coloured warning badge.
   * Values: ``"safe"`` (no badge), ``"read"``, ``"write"``,
   * ``"network"``, ``"exec"``.  ``null`` / undefined for
   * non-tool steps and for legacy rows that pre-date the field.
   */
  risk?: "safe" | "read" | "write" | "network" | "exec" | null;
}

export interface Run {
  id: string;
  agent_id: string;
  input: string;
  output: string;
  status: "pending" | "running" | "success" | "failed" | "paused";
  step_count: number;
  total_tokens: number;
  started_at: string;
  finished_at?: string;
  error?: string;
}

export interface RunDetail extends Run {
  trace: Step[];
}

/**
 * Server-pushed WebSocket message for the live-trace stream.
 *
 * 0.1.2 #10 — see ``app.api.ws`` for the wire protocol.  The
 * ``payload`` of a ``run.step`` is a :class:`Step`; a
 * ``run.snapshot`` carries the full :class:`RunDetail` in ``run``;
 * a ``run.complete`` carries the terminal summary fields.
 */
export type RunStreamMessage =
  | ({ event: "run.snapshot"; run: RunDetail })
  | ({ event: "run.step"; run_id: string; agent_id: string } & Step)
  | ({
      event: "run.complete";
      run_id: string;
      agent_id: string;
      status: Run["status"];
      output: string;
      total_tokens: number;
      duration_ms: number;
      step_count: number;
      error?: string;
    })
  | ({ event: "run.error"; message: string });

export interface Memory {
  id: string;
  agent_id: string;
  content: string;
  kind: string;
  importance: number;
  created_at: string;
  meta: Record<string, unknown>;
}

export interface EvalCase {
  input: string;
  expected: string;
}

/**
 * 0.1.3 #4 -- the per-case payload returned by ``GET /api/.../evals/{id}``.
 * Mirrors :class:`app.api.schemas.EvalCaseOut`.
 */
export interface EvalCaseResult extends EvalCase {
  score: number;
  passed: boolean;
  error?: string | null;
  duration_ms?: number;
  tokens?: number;
}

export interface EvalRun {
  id: string;
  agent_id: string;
  dataset: string;
  scorer: string;
  pass_threshold: number;
  status: "pending" | "running" | "completed" | "failed";
  total: number;
  passed: number;
  failed: number;
  average_score: number;
  duration_ms: number;
  started_at: string;
  finished_at?: string;
  /**
   * Per-case results.  Present on the detail endpoint; ``list`` may
   * omit it for older back-ends -- callers that need it should fetch
   * the run explicitly with :func:`Evals.get`.
   */
  cases?: EvalCaseResult[];
}

// --- Agent endpoints -------------------------------------------------------

export const Agents = {
  list: () => api.get<Agent[]>("/agents").then((r) => r.data),
  get: (id: string) => api.get<Agent>(`/agents/${id}`).then((r) => r.data),
  create: (data: Partial<Agent>) => api.post<Agent>("/agents", data).then((r) => r.data),
  update: (id: string, data: Partial<Agent>) =>
    api.patch<Agent>(`/agents/${id}`, data).then((r) => r.data),
  remove: (id: string) => api.delete(`/agents/${id}`).then((r) => r.status),
};

export const Tools = {
  list: () => api.get<ToolSpec[]>("/tools").then((r) => r.data),
};

// --- Run endpoints ---------------------------------------------------------

export const Runs = {
  list: (agentId: string) =>
    api.get<Run[]>(`/agents/${agentId}/runs`).then((r) => r.data),
  get: (agentId: string, runId: string) =>
    api.get<RunDetail>(`/agents/${agentId}/runs/${runId}`).then((r) => r.data),
  start: (agentId: string, input: string, asyncRun: boolean = false) =>
    api
      .post<Run>(`/agents/${agentId}/runs`, { input, async_run: asyncRun })
      .then((r) => r.data),
  pause: (agentId: string, runId: string) =>
    api
      .post<RunDetail>(`/agents/${agentId}/runs/${runId}/pause`)
      .then((r) => r.data),
  resume: (agentId: string, runId: string) =>
    api
      .post<RunDetail>(`/agents/${agentId}/runs/${runId}/resume`)
      .then((r) => r.data),
  /**
   * Open a live-trace WebSocket for ``runId`` (0.1.2 #10).
   *
   * The caller is responsible for ``onmessage`` / ``onclose``
   * handling and for calling ``ws.close()`` on unmount.  The
   * returned socket starts in CONNECTING state; the first
   * ``run.snapshot`` frame will arrive as soon as the server
   * accepts the connection.
   */
  stream: (agentId: string, runId: string): WebSocket =>
    new WebSocket(streamUrl(agentId, runId)),
};

// --- Memory endpoints -------------------------------------------------------

export const Memory = {
  list: (agentId: string) =>
    api.get<Memory[]>(`/agents/${agentId}/memory`).then((r) => r.data),
  add: (
    agentId: string,
    data: { content: string; kind?: string; importance?: number },
  ) =>
    api
      .post<Memory>(`/agents/${agentId}/memory`, {
        content: data.content,
        kind: data.kind ?? "manual",
        importance: data.importance ?? 0.5,
        metadata: {},
      })
      .then((r) => r.data),
  remove: (agentId: string, memoryId: string) =>
    api
      .delete(`/agents/${agentId}/memory/${memoryId}`)
      .then((r) => r.status),
};

// --- Eval endpoints --------------------------------------------------------

export const Evals = {
  list: (agentId: string) =>
    api.get<EvalRun[]>(`/agents/${agentId}/evals`).then((r) => r.data),
  get: (agentId: string, evalId: string) =>
    api.get<EvalRun>(`/agents/${agentId}/evals/${evalId}`).then((r) => r.data),
  start: (
    agentId: string,
    data: { dataset_name: string; scorer: string; pass_threshold: number; cases: EvalCase[] },
  ) => api.post<EvalRun>(`/agents/${agentId}/evals`, data).then((r) => r.data),
};
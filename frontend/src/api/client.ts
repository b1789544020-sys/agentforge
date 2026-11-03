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
export const API_TOKEN = (import.meta.env.VITE_API_TOKEN ?? "").trim();
const JWT_STORAGE_KEY = "agentforge.jwt";

export function getAccessToken(): string {
  return localStorage.getItem(JWT_STORAGE_KEY) ?? "";
}
export function setAccessToken(token: string): void {
  if (token) localStorage.setItem(JWT_STORAGE_KEY, token);
  else localStorage.removeItem(JWT_STORAGE_KEY);
}

export const api = axios.create({
  baseURL: API_BASE,
  timeout: 60_000,
  headers: API_TOKEN ? { "X-API-Key": API_TOKEN } : undefined,
});
api.interceptors.request.use((config) => {
  const jwt = getAccessToken();
  if (jwt) config.headers.Authorization = `Bearer ${jwt}`;
  return config;
});

/** Browser-safe WebSocket credentials that never put the token in a URL. */
export function streamProtocols(): string[] | undefined {
  const token = getAccessToken() || API_TOKEN;
  if (!token) return undefined;
  const bytes = new TextEncoder().encode(token);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const encoded = btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return ["agentforge", `bearer.${encoded}`];
}

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
  team_id?: string | null;
  name: string;
  description?: string;
  system_prompt: string;
  tools: string[];
  model: string;
  provider?: string;
  config?: Record<string, unknown>;
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
  status: "pending" | "running" | "success" | "failed" | "paused" | "cancelled" | "terminated";
  step_count: number;
  total_tokens: number;
  duration_ms: number;
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
  score?: number | null;
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
  status: "pending" | "running" | "cancelling" | "completed" | "failed" | "cancelled";
  priority: number;
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
  evaluate: (agentId: string, runId: string, data: { expected?: string; scorer_name?: string } = {}) =>
    api.post<EvalRun>(`/agents/${agentId}/runs/${runId}/evaluate`, data).then((r) => r.data),
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
    new WebSocket(streamUrl(agentId, runId), streamProtocols()),
};

// --- Memory endpoints -------------------------------------------------------

export const Memory = {
  list: (agentId: string) =>
    api.get<Memory[]>(`/agents/${agentId}/memory`).then((r) => r.data),
  search: (agentId: string, q: string, mode: "exact" | "semantic" | "hybrid", topK = 10) =>
    api.get<Memory[]>(`/agents/${agentId}/memory`, { params: { q, mode, top_k: topK } }).then((r) => r.data),
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
  cancel: (agentId: string, evalId: string) =>
    api.post<EvalRun>(`/agents/${agentId}/evals/${evalId}/cancel`).then((r) => r.data),
};

export interface AgentVersion {
  id: string;
  agent_id: string;
  version_number: number;
  gate_status: "pending" | "passed" | "failed" | "bypassed";
  snapshot: Record<string, unknown>;
  created_at: string;
}

export interface EvalSuite {
  id: string;
  name: string;
  description?: string | null;
  agent_id?: string | null;
  case_ids: string[];
  pass_threshold: number;
  scorer: string;
  enabled: boolean;
  auto_on_save: boolean;
}

export interface GateCaseResult {
  case_id: string;
  input: string;
  expected?: string | null;
  output: string;
  score: number;
  passed: boolean;
  error?: string | null;
}

export interface GateDiff {
  case_id: string;
  change: "new_failure" | "fixed" | "unchanged" | "new_case" | "removed";
  previous_passed?: boolean | null;
  current_passed?: boolean | null;
  answer_diff: string;
}

export interface GateRun {
  id: string;
  agent_version_id: string;
  suite_id: string;
  baseline_gate_id?: string | null;
  trigger_source: "save" | "manual" | "scheduled";
  status: "pending" | "running" | "passed" | "failed";
  pass_rate: number;
  threshold: number;
  failed_case_ids: string[];
  details: GateCaseResult[];
  diff: GateDiff[];
  error?: string | null;
  created_at: string;
}

export const QualityGates = {
  versions: (agentId: string) =>
    api.get<AgentVersion[]>(`/agents/${agentId}/versions`).then((r) => r.data),
  suites: (agentId: string) =>
    api.get<EvalSuite[]>("/suites", { params: { agent_id: agentId } }).then((r) => r.data),
  createSuite: (data: { name: string; agent_id: string; case_ids: string[]; pass_threshold: number }) =>
    api.post<EvalSuite>("/suites", { ...data, auto_on_save: false }).then((r) => r.data),
  list: (agentId: string, suiteId?: string) =>
    api.get<GateRun[]>(`/agents/${agentId}/gates`, { params: suiteId ? { suite_id: suiteId } : {} }).then((r) => r.data),
  get: (agentId: string, gateId: string) =>
    api.get<GateRun>(`/agents/${agentId}/gates/${gateId}`).then((r) => r.data),
  trigger: (agentId: string, agentVersionId: string, suiteId: string) =>
    api.post<GateRun>(`/agents/${agentId}/gates`, {
      agent_version_id: agentVersionId, suite_id: suiteId,
    }).then((r) => r.data),
};

export interface Deployment {
  id: string;
  agent_id: string;
  agent_version_id: string;
  name: string;
  status: "active" | "stopped";
  token_revoked: boolean;
  token_prefix: string;
  version_history: string[];
  token?: string;
  api_url?: string;
  page_url?: string;
  embed_url?: string;
}

export interface AgentTemplate {
  id: string;
  name: string;
  description?: string;
  category: string;
  version_number: number;
  builtin: boolean;
}

export const Deliveries = {
  list: () => api.get<Deployment[]>("/deployments").then((r) => r.data),
  publish: (agentVersionId: string, name: string) =>
    api.post<Deployment>("/deployments", { agent_version_id: agentVersionId, name }).then((r) => r.data),
  action: (id: string, action: "start" | "stop" | "rollback" | "revoke") =>
    api.post<Deployment>(`/deployments/${id}/${action}`).then((r) => r.data),
  rotate: (id: string) => api.post<Deployment>(`/deployments/${id}/rotate-token`).then((r) => r.data),
};

export const Templates = {
  list: () => api.get<AgentTemplate[]>("/templates").then((r) => r.data),
  instantiate: (id: string) => api.post<{ agent: Agent }>(`/templates/${id}/instantiate`, {}).then((r) => r.data),
};

export interface KnowledgeBase {
  id: string;
  name: string;
  description?: string | null;
}

export interface KnowledgeDocument {
  id: string;
  filename: string;
  status: "processing" | "ready" | "failed";
  error?: string | null;
  chunks: number;
}

export interface KnowledgeHit {
  content: string;
  document: string;
  section?: string | null;
  score: number;
}

export const Knowledge = {
  list: () => api.get<KnowledgeBase[]>("/knowledge-bases").then((r) => r.data),
  create: (name: string) => api.post<KnowledgeBase>("/knowledge-bases", { name }).then((r) => r.data),
  documents: (id: string) => api.get<KnowledgeDocument[]>(`/knowledge-bases/${id}/documents`).then((r) => r.data),
  upload: (id: string, file: File) => {
    const data = new FormData(); data.append("file", file);
    return api.post<KnowledgeDocument>(`/knowledge-bases/${id}/documents`, data).then((r) => r.data);
  },
  bind: (agentId: string, kbId: string) => api.put(`/agents/${agentId}/knowledge/${kbId}`),
  search: (kbId: string, agentId: string, q: string) =>
    api.get<KnowledgeHit[]>(`/knowledge-bases/${kbId}/search`, { params: { agent_id: agentId, q } }).then((r) => r.data),
};

export interface MetricSummary {
  requests: number;
  success: number;
  failed: number;
  rejected: number;
  failure_rate: number;
  total_tokens: number;
  average_duration_ms: number;
  p95_duration_ms: number;
}

export interface MetricsResponse {
  summary: MetricSummary;
  trends: ({ bucket: string } & MetricSummary)[];
  dimensions: Record<string, Record<string, MetricSummary>>;
}

export interface CostResponse {
  total: number;
  currency: string;
  unpriced_tokens: number;
  top_agents: { agent_id: string; cost: number }[];
  models: { model: string; cost: number }[];
  trends: { bucket: string; cost: number }[];
}

export interface FeedbackItem {
  id: string;
  run_id: string;
  rating: "positive" | "negative";
  expected_answer?: string | null;
  context_snapshot: { input?: string; output?: string; agent_version_id?: string };
  eval_case_id?: string | null;
}

export interface AlertRule {
  id: string;
  name: string;
  kind: "failure_rate" | "daily_cost" | "error_spike" | "gate_failures";
  threshold: number;
  enabled: boolean;
}

export interface AlertEvent {
  id: string;
  rule_id: string;
  status: "open" | "acknowledged";
  message: string;
  created_at: string;
}

export const Operations = {
  metrics: (params: Record<string, string>) =>
    api.get<MetricsResponse>("/operations/metrics", { params }).then((r) => r.data),
  cost: (params: Record<string, string>) =>
    api.get<CostResponse>("/operations/cost", { params }).then((r) => r.data),
  feedback: () => api.get<FeedbackItem[]>("/operations/feedback", { params: { rating: "negative" } }).then((r) => r.data),
  convertFeedback: (id: string, expectedAnswer: string, suiteId?: string) =>
    api.post(`/operations/feedback/${id}/convert`, { expected_answer: expectedAnswer, suite_id: suiteId || null }),
  rules: () => api.get<AlertRule[]>("/operations/alerts/rules").then((r) => r.data),
  createRule: (data: { name: string; kind: string; threshold: number }) =>
    api.post<AlertRule>("/operations/alerts/rules", data).then((r) => r.data),
  events: () => api.get<AlertEvent[]>("/operations/alerts/events").then((r) => r.data),
  evaluate: () => api.post("/operations/alerts/evaluate").then((r) => r.data),
  acknowledge: (id: string) => api.post<AlertEvent>(`/operations/alerts/events/${id}/ack`).then((r) => r.data),
};

export interface Team {
  id: string;
  org_id: string;
  name: string;
  role?: "super-admin" | "team-admin" | "developer" | "viewer" | null;
}

export interface TeamMember {
  id: string;
  team_id: string;
  user_id: string;
  email?: string | null;
  role: "team-admin" | "developer" | "viewer";
}

export interface AuditEntry {
  id: string;
  actor_id?: string | null;
  team_id?: string | null;
  action: string;
  target_type: string;
  target_id: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export const Teams = {
  list: () => api.get<Team[]>("/teams").then((r) => r.data),
  createOrg: (name: string) => api.post<{ id: string; name: string }>("/orgs", { name }).then((r) => r.data),
  create: (orgId: string, name: string) => api.post<Team>("/teams", { org_id: orgId, name }).then((r) => r.data),
  members: (teamId: string) => api.get<TeamMember[]>(`/teams/${teamId}/members`).then((r) => r.data),
  addMember: (teamId: string, email: string, role: string) =>
    api.post<TeamMember>(`/teams/${teamId}/members`, { email, role }).then((r) => r.data),
  setRole: (teamId: string, userId: string, role: string) =>
    api.patch<TeamMember>(`/teams/${teamId}/members/${userId}`, { role }).then((r) => r.data),
  removeMember: (teamId: string, userId: string) => api.delete(`/teams/${teamId}/members/${userId}`),
  audit: (teamId: string, action = "") =>
    api.get<AuditEntry[]>("/audit", { params: { team_id: teamId, action: action || undefined } }).then((r) => r.data),
};

export interface User {
  id: string | null;
  email: string | null;
  role: "admin" | "user";
  is_static?: boolean;
  memberships?: { team_id: string; role: "team-admin" | "developer" | "viewer" }[];
  permissions?: string[];
}

export const Auth = {
  login: (email: string, password: string) =>
    api.post<{ access_token: string; user: User }>("/auth/login", { email, password }).then((r) => {
      setAccessToken(r.data.access_token);
      return r.data.user;
    }),
  register: (email: string, password: string) =>
    api.post<{ access_token: string; user: User }>("/auth/register", { email, password }).then((r) => {
      setAccessToken(r.data.access_token);
      return r.data.user;
    }),
  me: () => api.get<User>("/auth/me").then((r) => r.data),
  logout: () => setAccessToken(""),
};

export interface PromptVersion {
  id: string;
  version: number;
  template: string;
  created_at: string;
}

export interface PromptTemplate {
  id: string;
  agent_id: string;
  name: string;
  description?: string;
  latest_version: number;
  versions: PromptVersion[];
}

export interface PromptComparisonSide {
  eval_run_id: string;
  version: number;
  average_score: number;
  pass_rate: number;
  passed: number;
  total: number;
}

export const Prompts = {
  list: (agentId: string) =>
    api.get<PromptTemplate[]>(`/agents/${agentId}/prompt-templates`).then((r) => r.data),
  create: (agentId: string, data: { name: string; template: string; description?: string }) =>
    api.post<PromptTemplate>(`/agents/${agentId}/prompt-templates`, data).then((r) => r.data),
  addVersion: (agentId: string, templateId: string, template: string) =>
    api.post<PromptVersion>(`/agents/${agentId}/prompt-templates/${templateId}/versions`, { template }).then((r) => r.data),
  bind: (agentId: string, templateId: string, version: number | null, variables: Record<string, unknown>) =>
    api.post(`/agents/${agentId}/prompt-templates/${templateId}/bind`, { version, variables }).then((r) => r.data),
  compare: (agentId: string, templateId: string, data: {
    version_a: number; version_b: number; cases: EvalCase[]; scorer_name?: string;
    variables?: Record<string, unknown>;
  }) =>
    api.post<{
      version_a: PromptComparisonSide;
      version_b: PromptComparisonSide;
      delta: { average_score: number; pass_rate: number };
    }>(`/agents/${agentId}/prompt-templates/${templateId}/compare`, data).then((r) => r.data),
};
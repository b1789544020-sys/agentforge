import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useAgentStore } from "../stores/agentStore";
import {
  Memory as MemoryApi,
  Runs,
  type Memory,
  type Run,
  type RunDetail,
  type RunStreamMessage,
  type Step,
} from "../api/client";
import { useRunStream } from "../hooks/useRunStream";
import TraceTree from "../components/TraceTree";
import TokenGauge from "../components/TokenGauge";

const TOKEN_BUDGET = 8000;
const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 5 * 60 * 1000; // 5 minutes

type StatusKey = "pending" | "running" | "paused" | "success" | "failed";

function statusKey(s: string): StatusKey {
  if (
    s === "pending" ||
    s === "running" ||
    s === "paused" ||
    s === "success" ||
    s === "failed"
  ) {
    return s;
  }
  return "failed";
}

function isTerminal(s: string): boolean {
  return s === "success" || s === "failed";
}

export default function AgentRun() {
  const { t } = useTranslation();
  const { agents, loadAgents } = useAgentStore();
  const [agentId, setAgentId] = useState<string>("");
  const [input, setInput] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [activeRun, setActiveRun] = useState<RunDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [asyncMode, setAsyncMode] = useState(false);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [newMemory, setNewMemory] = useState("");
  const [memorySaving, setMemorySaving] = useState(false);

  // Track the active poll so we can cancel it on unmount or on
  // switching agents.
  const pollRef = useRef<{ cancelled: boolean; timer?: number } | null>(null);

  useEffect(() => {
    loadAgents();
  }, [loadAgents]);

  useEffect(() => {
    if (agents.length && !agentId) {
      setAgentId(agents[0].id);
    }
  }, [agents, agentId]);

  useEffect(() => {
    if (!agentId) {
      setRuns([]);
      setMemories([]);
      return;
    }
    Runs.list(agentId).then(setRuns).catch((e) => setError(String(e)));
    MemoryApi.list(agentId)
      .then(setMemories)
      .catch((e) => {
        setMemories([]);
        setError(String(e));
      });
  }, [agentId]);

  // Cancel any in-flight poll on unmount or agent change.
  useEffect(() => {
    return () => {
      if (pollRef.current) {
        pollRef.current.cancelled = true;
        if (pollRef.current.timer) window.clearTimeout(pollRef.current.timer);
      }
    };
  }, [agentId]);

  const refresh = async (runId: string) => {
    if (!agentId) return;
    try {
      const detail = await Runs.get(agentId, runId);
      setActiveRun(detail);
    } catch (e) {
      setError(String(e));
    }
  };

  /**
   * Polls a run until it leaves the {pending, running, paused} state set,
   * refreshing the activeRun and the runs list when it does.  Stops on
   * unmount, on agent change, or after POLL_TIMEOUT_MS.
   */
  const startPoll = (runId: string) => {
    if (pollRef.current) {
      pollRef.current.cancelled = true;
      if (pollRef.current.timer) window.clearTimeout(pollRef.current.timer);
    }
    const state = { cancelled: false, timer: undefined as number | undefined };
    pollRef.current = state;
    const startedAt = Date.now();

    const tick = async () => {
      if (state.cancelled || !agentId) return;
      if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
        setError(t("run.async_submitted") + " (timeout)");
        setRunning(false);
        return;
      }
      try {
        const detail = await Runs.get(agentId, runId);
        if (state.cancelled) return;
        setActiveRun(detail);
        setRuns((rs) => rs.map((x) => (x.id === runId ? detail : x)));
        if (!isTerminal(detail.status)) {
          state.timer = window.setTimeout(tick, POLL_INTERVAL_MS);
        } else {
          setRunning(false);
        }
      } catch (e) {
        if (state.cancelled) return;
        setError(String(e));
        setRunning(false);
      }
    };
    // Fire one tick immediately so the UI updates without waiting
    // POLL_INTERVAL_MS for the first poll.
    void tick();
  };

  const onRun = async () => {
    if (!agentId || !input.trim()) return;
    setRunning(true);
    setError(null);
    try {
      // 0.1.2 #10 — the live WebSocket trace only makes sense
      // when the run is async (the POST has to return before the
      // loop starts producing steps).  The "Async" checkbox in
      // the UI controls this; when unchecked we still go async
      // under the hood but the loop's result is the only thing
      // the user ever sees — the live-stream hook will close the
      // socket as soon as the terminal frame arrives.
      //
      // We always submit as async to keep the wire shape
      // uniform; ``Runs.start(agentId, input, true)`` returns
      // the run row immediately and the background task does
      // the actual work.  The legacy "synchronous" path is
      // still available by setting ``asyncMode = false`` and
      // calling ``Runs.get`` after the POST (left for future
      // use; the WS hook already covers it).
      const run = await Runs.start(agentId, input.trim(), true);
      setRuns((r) => [run, ...r]);
      setActiveRun({ ...run, trace: [] });
      setInput("");
      // The hook subscribes automatically when ``activeRun.id``
      // becomes truthy AND ``running`` is true; we set both
      // above so the next render will open the socket.  When
      // ``asyncMode`` is unchecked we also kick a single poll
      // so the legacy sync UX still gets a final trace.
      if (!asyncMode) {
        const detail = await Runs.get(agentId, run.id);
        setActiveRun(detail);
        setRuns((rs) => rs.map((x) => (x.id === run.id ? detail : x)));
        setRunning(false);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("run.run_failed"));
      setRunning(false);
    }
  };

  // ---------------------------------------------------------------
  // 0.1.2 #10 — live WebSocket trace (replaces the old 1.5s poll
  // loop for the async case).  ``onEvent`` is invoked once per
  // server frame; the hook manages the socket lifecycle.
  // ---------------------------------------------------------------
  const handleStreamEvent = (msg: RunStreamMessage) => {
    switch (msg.event) {
      case "run.snapshot": {
        // Server's view of the run + trace at the moment we
        // connected.  Replace ``activeRun`` wholesale.
        setActiveRun(msg.run);
        setRuns((rs) => rs.map((x) => (x.id === msg.run.id ? msg.run : x)));
        return;
      }
      case "run.step": {
        // A single new step.  Merge by id; if the step is
        // already in the trace (re-snapshot race) skip it.  The
        // ``run.step`` event shape is a :class:`Step` with two
        // extra routing fields (``event`` / ``run_id`` /
        // ``agent_id``); we strip those off for the trace list.
        const incoming = msg;
        setActiveRun((prev) => {
          if (!prev || prev.id !== incoming.run_id) return prev;
          const trace = prev.trace ?? [];
          if (trace.some((s) => s.id === incoming.id)) return prev;
          const step: Step = {
            id: incoming.id,
            index: incoming.index,
            kind: incoming.kind,
            name: incoming.name,
            content: incoming.content,
            payload: incoming.payload,
            duration_ms: incoming.duration_ms,
            tokens: incoming.tokens,
            status: incoming.status,
            created_at: incoming.created_at,
          };
          return { ...prev, trace: [...trace, step] };
        });
        return;
      }
      case "run.complete": {
        setActiveRun((prev) =>
          prev && prev.id === msg.run_id
            ? {
                ...prev,
                status: msg.status,
                output: msg.output,
                total_tokens: msg.total_tokens,
                duration_ms: msg.duration_ms,
                step_count: msg.step_count,
                error: msg.error,
              }
            : prev,
        );
        setRuns((rs) =>
          rs.map((x) =>
            x.id === msg.run_id
              ? {
                  ...x,
                  status: msg.status,
                  output: msg.output,
                  total_tokens: msg.total_tokens,
                  duration_ms: msg.duration_ms,
                  step_count: msg.step_count,
                  error: msg.error,
                }
              : x,
          ),
        );
        setRunning(false);
        return;
      }
      case "run.error": {
        setError(msg.message);
        setRunning(false);
        return;
      }
    }
  };

  // Open the live stream while a run is in flight.  When the
  // server emits ``run.complete`` we set ``running = false`` and
  // the hook tears the socket down on the next render.  If the
  // socket dies unexpectedly (network drop, server restart), the
  // close callback kicks a one-shot poll as a safety net.
  useRunStream(
    activeRun?.agent_id ?? null,
    running && activeRun ? activeRun.id : null,
    handleStreamEvent,
    ({ hadComplete }) => {
      if (!hadComplete && running && activeRun) {
        startPoll(activeRun.id);
      } else {
        setRunning(false);
      }
    },
  );

  const onPause = async () => {
    if (!agentId || !activeRun) return;
    try {
      const detail = await Runs.pause(agentId, activeRun.id);
      setActiveRun(detail);
      setRuns((rs) => rs.map((x) => (x.id === detail.id ? detail : x)));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const onResume = async () => {
    if (!agentId || !activeRun) return;
    try {
      const detail = await Runs.resume(agentId, activeRun.id);
      setActiveRun(detail);
      setRuns((rs) => rs.map((x) => (x.id === detail.id ? detail : x)));
      // After resume the run is running again; the live stream
      // hook will re-open on the next render (we just set
      // ``activeRun`` + ``running`` is already true because the
      // row came back as "running").  We also kick a one-shot
      // poll as a safety net in case the WS fails to connect.
      startPoll(detail.id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const onAddMemory = async () => {
    if (!agentId || !newMemory.trim()) return;
    setMemorySaving(true);
    try {
      const created = await MemoryApi.add(agentId, { content: newMemory.trim() });
      setMemories((m) => [created, ...m]);
      setNewMemory("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setMemorySaving(false);
    }
  };

  const onDeleteMemory = async (id: string) => {
    if (!agentId) return;
    try {
      await MemoryApi.remove(agentId, id);
      setMemories((m) => m.filter((x) => x.id !== id));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <div className="page">
      <h2>{t("run.title")}</h2>
      {error && <div className="banner err" role="alert">{error}</div>}

      <section className="card">
        <div className="row" style={{ gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
          <label style={{ minWidth: 200 }}>
            {t("run.agent")}
            <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              {agents.length === 0 && <option value="">{t("run.no_agents")}</option>}
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
          <label style={{ flex: 1, minWidth: 260 }}>
            {t("run.input")}
            <textarea
              rows={3}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={t("run.input_placeholder")}
            />
          </label>
          <button onClick={onRun} disabled={!agentId || !input.trim() || running} className="lg">
            {running ? t("run.running") : t("run.run")}
          </button>
        </div>
        <div style={{ marginTop: 10 }}>
          <label className="row" style={{ gap: 8 }}>
            <input
              type="checkbox"
              checked={asyncMode}
              onChange={(e) => setAsyncMode(e.target.checked)}
            />
            <span>{t("run.async_mode")}</span>
          </label>
        </div>
      </section>
      <div className="grid-2">
        <section className="card">
          <h3>{t("run.trace")}</h3>
          {activeRun ? (
            <>
              <div className="muted row" style={{ gap: 8, flexWrap: "wrap" }}>
                <span>
                  {t("run.run_id", { id: activeRun.id.slice(0, 8) })} ·{" "}
                  <span className={`badge ${activeRun.status}`}>
                    {t(`run.status_${statusKey(activeRun.status)}`, {
                      defaultValue: activeRun.status,
                    })}
                  </span>
                </span>
                {activeRun.status === "running" || activeRun.status === "pending" ? (
                  <button onClick={onPause} className="secondary" style={{ marginLeft: "auto" }}>
                    {t("run.pause")}
                  </button>
                ) : null}
                {activeRun.status === "paused" ? (
                  <button onClick={onResume} className="secondary" style={{ marginLeft: "auto" }}>
                    {t("run.resume")}
                  </button>
                ) : null}
              </div>
              <div style={{ margin: "12px 0" }}>
                <TokenGauge used={activeRun.total_tokens} budget={TOKEN_BUDGET} />
              </div>
              <TraceTree steps={activeRun.trace} />
            </>
          ) : (
            <div className="empty">
              <div className="empty-icon" aria-hidden="true">🪜</div>
              <div className="empty-title">{t("run.trace_empty")}</div>
            </div>
          )}
        </section>
        <section className="card">
          <h3>{t("run.history")}</h3>
          {runs.length === 0 ? (
            <div className="empty">
              <div className="empty-icon" aria-hidden="true">📭</div>
              <div className="empty-title">{t("run.no_runs")}</div>
            </div>
          ) : (
            <ul className="history-list">
              {runs.map((r) => (
                <li
                  key={r.id}
                  className={activeRun?.id === r.id ? "active" : ""}
                  onClick={() => void refresh(r.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      void refresh(r.id);
                    }
                  }}
                  tabIndex={0}
                  role="button"
                  aria-label={typeof r.input === "string" ? r.input.slice(0, 80) : "Run"}
                >
                  <div className="hl-input">
                    {typeof r.input === "string" ? r.input.slice(0, 60) : ""}
                  </div>
                  <div className="muted">
                    {t("run.run_summary", {
                      status: t(`run.status_${statusKey(r.status)}`, {
                        defaultValue: r.status,
                      }),
                      steps: r.step_count,
                      tokens: r.total_tokens,
                    })}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
      <section className="card">
        <h3>
          <span>{t("run.memory_title")}</span>
          <span className="muted" style={{ fontWeight: "normal", fontSize: 12 }}>
            {t("run.memory_count", { count: memories.length })}
          </span>
        </h3>
        <div className="row" style={{ gap: 8, marginBottom: 12 }}>
          <input
            type="text"
            value={newMemory}
            onChange={(e) => setNewMemory(e.target.value)}
            placeholder={t("run.memory_add_placeholder")}
            style={{ flex: 1 }}
            disabled={!agentId || memorySaving}
            onKeyDown={(e) => {
              if (e.key === "Enter") void onAddMemory();
            }}
          />
          <button
            onClick={onAddMemory}
            disabled={!agentId || !newMemory.trim() || memorySaving}
            className="secondary"
          >
            {t("run.memory_add")}
          </button>
        </div>
        {memories.length === 0 ? (
          <div className="empty">
            <div className="empty-icon" aria-hidden="true">🧠</div>
            <div className="empty-title">{t("run.memory_empty")}</div>
          </div>
        ) : (
          <ul className="memory-list">
            {memories.map((m) => (
              <li key={m.id}>
                <div className="mem-content">
                  <div>{m.content}</div>
                  <div className="mem-meta">
                    {t("run.memory_kind")}: {m.kind} ·{" "}
                    {t("run.memory_importance")}: {m.importance.toFixed(2)}
                  </div>
                </div>
                <button onClick={() => void onDeleteMemory(m.id)} className="danger icon-only" title={t("run.memory_delete")}>
                  🗑
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

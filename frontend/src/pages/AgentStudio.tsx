import axios from "axios";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Agents,
  Runs,
  type AgentDefinition,
  type ResourceMap,
  type RunDetail,
  type SecretRef,
} from "../api/client";
import TraceTree from "../components/TraceTree";
import { useAgentStore } from "../stores/agentStore";

type Side = "a" | "b";
type RunState = Record<Side, RunDetail | null>;
type SideError = Record<Side, string | null>;
type SideRunning = Record<Side, boolean>;

const EMPTY_RUNS: RunState = { a: null, b: null };
const DEFAULT_RESOURCE_MAP = JSON.stringify({ knowledge_bases: {}, prompts: {} }, null, 2);
const POLL_INTERVAL_MS = 500;
const POLL_TIMEOUT_MS = 5 * 60 * 1000;

function parseObject(source: string): Record<string, unknown> {
  const value: unknown = JSON.parse(source);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("JSON_OBJECT_EXPECTED");
  }
  return value as Record<string, unknown>;
}

function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail: unknown = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (detail) return JSON.stringify(detail);
  }
  return error instanceof Error ? error.message : String(error);
}

function isStaleVersion(error: unknown): boolean {
  if (!axios.isAxiosError(error) || error.response?.status !== 409) return false;
  const detail: unknown = error.response.data?.detail;
  return !!detail && typeof detail === "object" && "error" in detail && detail.error === "stale_base_version";
}

function editableSecretRefs(source: string, fallback: SecretRef[]): SecretRef[] {
  try {
    const refs = parseObject(source).secret_refs;
    if (!Array.isArray(refs)) return fallback;
    return refs.filter((row): row is SecretRef => (
      !!row && typeof row === "object" &&
      typeof row.ref === "string" && typeof row.path === "string" &&
      typeof row.required === "boolean"
    ));
  } catch {
    return fallback;
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

export default function AgentStudio() {
  const { t } = useTranslation();
  const { agents, loadAgents } = useAgentStore();
  const [agentId, setAgentId] = useState("");
  const [reloadIndex, setReloadIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [definition, setDefinition] = useState<AgentDefinition | null>(null);
  const [definitionText, setDefinitionText] = useState("");
  const [baseVersionId, setBaseVersionId] = useState("");
  const [versionNumber, setVersionNumber] = useState(0);
  const [versionRows, setVersionRows] = useState<{ id: string; version_number: number }[]>([]);
  const [resourceMapText, setResourceMapText] = useState(DEFAULT_RESOURCE_MAP);
  const [secretInputs, setSecretInputs] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [versionA, setVersionA] = useState("");
  const [versionB, setVersionB] = useState("");
  const [input, setInput] = useState("");
  const [runs, setRuns] = useState<RunState>(EMPTY_RUNS);
  const [runErrors, setRunErrors] = useState<SideError>({ a: null, b: null });
  const [running, setRunning] = useState<SideRunning>({ a: false, b: false });
  const [traceSide, setTraceSide] = useState<Side>("a");
  const generation = useRef(0);
  const busy = running.a || running.b;
  const secretRefs = useMemo(
    () => editableSecretRefs(definitionText, definition?.secret_refs ?? []),
    [definitionText, definition],
  );

  useEffect(() => {
    void loadAgents();
  }, [loadAgents]);

  useEffect(() => {
    if (agents.length && !agents.some((agent) => agent.id === agentId)) {
      setAgentId(agents[0].id);
    }
  }, [agents, agentId]);

  useEffect(() => {
    const ticket = ++generation.current;
    let cancelled = false;
    setRuns(EMPTY_RUNS);
    setRunErrors({ a: null, b: null });
    setRunning({ a: false, b: false });
    setNotice(null);
    if (!agentId) {
      setDefinition(null);
      setDefinitionText("");
      setVersionRows([]);
      setVersionA("");
      setVersionB("");
      setLoading(false);
      return () => { cancelled = true; };
    }

    setLoading(true);
    setError(null);
    setDefinition(null);
    setDefinitionText("");
    setBaseVersionId("");
    setVersionNumber(0);
    setVersionRows([]);
    setVersionA("");
    setVersionB("");
    setSecretInputs({});
    setWarnings([]);
    setDirty(false);
    Promise.all([Agents.definition(agentId), Agents.versions(agentId)])
      .then(([current, rows]) => {
        if (cancelled || ticket !== generation.current) return;
        const sorted = [...rows].sort((left, right) => right.version_number - left.version_number);
        setDefinition(current.definition);
        setDefinitionText(JSON.stringify(current.definition, null, 2));
        setBaseVersionId(current.base_version_id);
        setVersionNumber(current.version_number);
        setVersionRows(sorted);
        setVersionA(current.base_version_id);
        setVersionB(sorted.find((row) => row.id !== current.base_version_id)?.id ?? current.base_version_id);
        setResourceMapText(DEFAULT_RESOURCE_MAP);
        setSecretInputs({});
        setWarnings([]);
        setDirty(false);
      })
      .catch((loadError: unknown) => {
        if (!cancelled && ticket === generation.current) {
          setError(`${t("studio.load_failed")}: ${errorMessage(loadError)}`);
        }
      })
      .finally(() => {
        if (!cancelled && ticket === generation.current) setLoading(false);
      });
    return () => {
      cancelled = true;
      generation.current += 1;
    };
  }, [agentId, reloadIndex, t]);

  const onDefinitionChange = (value: string) => {
    setDefinitionText(value);
    setDirty(true);
    setNotice(null);
  };

  const onFormat = () => {
    try {
      const formatted = JSON.stringify(parseObject(definitionText), null, 2);
      setDefinitionText(formatted);
      setError(null);
    } catch (formatError: unknown) {
      setError(t("studio.invalid_json", {
        field: t("studio.definition"),
        message: errorMessage(formatError),
      }));
    }
  };

  const onSave = async () => {
    if (!agentId || !baseVersionId || loading) return;
    let parsedDefinition: AgentDefinition;
    let resourceMap: ResourceMap;
    try {
      parsedDefinition = parseObject(definitionText) as unknown as AgentDefinition;
    } catch (parseError: unknown) {
      setError(errorMessage(parseError) === "JSON_OBJECT_EXPECTED"
        ? t("studio.definition_object")
        : t("studio.invalid_json", { field: t("studio.definition"), message: errorMessage(parseError) }));
      return;
    }
    try {
      resourceMap = parseObject(resourceMapText) as unknown as ResourceMap;
    } catch (parseError: unknown) {
      setError(errorMessage(parseError) === "JSON_OBJECT_EXPECTED"
        ? t("studio.resource_map_object")
        : t("studio.invalid_json", { field: t("studio.resource_map"), message: errorMessage(parseError) }));
      return;
    }

    const ticket = generation.current;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const secretPayload = Object.fromEntries(
        Object.entries(secretInputs).filter(([, value]) => value.length > 0),
      );
      const current = await Agents.saveDefinition(agentId, {
        definition: parsedDefinition,
        base_version_id: baseVersionId,
        resource_map: resourceMap,
        secret_inputs: secretPayload,
      });
      if (ticket !== generation.current) return;
      const previousBaseVersionId = baseVersionId;
      setDefinition(current.definition);
      setDefinitionText(JSON.stringify(current.definition, null, 2));
      setBaseVersionId(current.base_version_id);
      setVersionNumber(current.version_number);
      setSecretInputs({});
      setWarnings(current.warnings.map((warning) => warning.code));
      setDirty(false);
      setVersionA(current.base_version_id);
      setVersionB(previousBaseVersionId);
      setNotice(t("studio.saved", { number: current.version_number }));

      try {
        const rows = await Agents.versions(agentId);
        if (ticket !== generation.current) return;
        setVersionRows([...rows].sort((left, right) => right.version_number - left.version_number));
      } catch (refreshError: unknown) {
        if (ticket === generation.current) {
          setVersionRows((rows) => [
            { id: current.base_version_id, version_number: current.version_number },
            ...rows.filter((row) => row.id !== current.base_version_id),
          ]);
          setError(`${t("studio.load_failed")}: ${errorMessage(refreshError)}`);
        }
      }
    } catch (saveError: unknown) {
      if (ticket !== generation.current) return;
      setError(isStaleVersion(saveError)
        ? t("studio.stale")
        : `${t("studio.save_failed")}: ${errorMessage(saveError)}`);
    } finally {
      if (ticket === generation.current) setSaving(false);
    }
  };

  const updateRun = (side: Side, run: RunDetail, ticket: number) => {
    if (ticket !== generation.current) return;
    setRuns((current) => ({ ...current, [side]: run }));
  };

  const executeRun = async (side: Side, selectedVersionId: string, sharedInput: string, ticket: number) => {
    try {
      const started = await Runs.startVersion(agentId, selectedVersionId, sharedInput);
      if (ticket !== generation.current) return;
      updateRun(side, { ...started, trace: [] }, ticket);
      const deadline = Date.now() + POLL_TIMEOUT_MS;
      while (Date.now() < deadline) {
        if (ticket !== generation.current) return;
        const current = await Runs.get(agentId, started.id);
        updateRun(side, current, ticket);
        if (current.status !== "pending" && current.status !== "running") return;
        await delay(POLL_INTERVAL_MS);
      }
      throw new Error(t("studio.poll_timeout"));
    } catch (runError: unknown) {
      if (ticket === generation.current) {
        setRunErrors((current) => ({
          ...current,
          [side]: `${t("studio.run_failed")}: ${errorMessage(runError)}`,
        }));
      }
    } finally {
      if (ticket === generation.current) {
        setRunning((current) => ({ ...current, [side]: false }));
      }
    }
  };

  const startRuns = (compare: boolean) => {
    const sharedInput = input.trim();
    if (!agentId || !versionA || !sharedInput || loading || busy) return;
    if (compare && (!versionB || versionA === versionB)) {
      setError(t("studio.select_distinct"));
      return;
    }
    const ticket = ++generation.current;
    setError(null);
    setNotice(null);
    setRuns(EMPTY_RUNS);
    setRunErrors({ a: null, b: null });
    setRunning({ a: true, b: compare });
    setTraceSide("a");
    void executeRun("a", versionA, sharedInput, ticket);
    if (compare) void executeRun("b", versionB, sharedInput, ticket);
  };

  const displayVersion = (id: string | null | undefined) => {
    const row = versionRows.find((version) => version.id === id);
    return row ? `v${row.version_number}` : id?.slice(0, 8) || "—";
  };

  const activeRun = runs[traceSide];

  return (
    <div className="page studio-page">
      <h2>{t("studio.title")}</h2>
      <p className="muted">{t("studio.subtitle")}</p>
      <div className="studio-toolbar">
        <label>
          {t("studio.agent")}
          <select aria-label={t("studio.agent")} value={agentId} disabled={saving || busy} onChange={(event) => setAgentId(event.target.value)}>
            {agents.length === 0 && <option value="">{t("studio.no_agents")}</option>}
            {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
          </select>
        </label>
        <button type="button" className="secondary" disabled={!agentId || loading || saving || busy} onClick={() => setReloadIndex((value) => value + 1)}>
          {t("studio.reload")}
        </button>
      </div>
      {error && <div className="banner err" role="alert">{error}</div>}
      {notice && <div className="banner ok" role="status">{notice}</div>}

      <div className="studio-grid">
        <div className="studio-column">
          <section className="card">
            <div className="studio-card-head">
              <h3>{t("studio.definition")}</h3>
              {versionNumber > 0 && <span className="badge accent">{t("studio.version_badge", { number: versionNumber })}</span>}
              {dirty && <span className="badge warn">{t("studio.unsaved")}</span>}
            </div>
            <label>
              <span className="muted">JSON</span>
              <textarea
                className="studio-definition"
                aria-label={t("studio.definition")}
                value={definitionText}
                disabled={!agentId || loading || saving || busy}
                spellCheck={false}
                onChange={(event) => onDefinitionChange(event.target.value)}
              />
            </label>
            <div className="studio-actions">
              <button type="button" className="secondary" disabled={!definitionText || saving || busy} onClick={onFormat}>{t("studio.format")}</button>
            </div>
            <details>
              <summary>{t("studio.resource_map")}</summary>
              <textarea
                className="studio-resource-map"
                aria-label={t("studio.resource_map")}
                value={resourceMapText}
                disabled={saving || busy}
                spellCheck={false}
                onChange={(event) => setResourceMapText(event.target.value)}
              />
            </details>
            {secretRefs.length > 0 && (
              <fieldset>
                <legend>{t("studio.secret_inputs")}</legend>
                {secretRefs.map((secret) => (
                  <label className="studio-secret" key={secret.ref}>
                    <code title={secret.path}>{secret.ref}{secret.required ? " *" : ""}</code>
                    <input
                      type="password"
                      autoComplete="off"
                      aria-label={t("studio.secret_value", { ref: secret.ref })}
                      value={secretInputs[secret.ref] ?? ""}
                      disabled={saving || busy}
                      onChange={(event) => setSecretInputs((current) => ({ ...current, [secret.ref]: event.target.value }))}
                    />
                  </label>
                ))}
              </fieldset>
            )}
            <div className="studio-actions">
              <button type="button" disabled={!agentId || !baseVersionId || loading || saving || busy} onClick={() => void onSave()}>
                {saving ? t("studio.saving") : t("studio.save")}
              </button>
            </div>
            {warnings.length > 0 && (
              <div><h4>{t("studio.warnings")}</h4><ul className="studio-warning-list">{warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></div>
            )}
          </section>
        </div>

        <div className="studio-column">
          <section className="card">
            <h3>{t("studio.debug")}</h3>
            <div className="studio-version-grid">
              <label>
                {t("studio.version_a")}
                <select aria-label={t("studio.version_a")} value={versionA} disabled={loading || busy || saving} onChange={(event) => setVersionA(event.target.value)}>
                  {versionRows.map((version) => <option key={version.id} value={version.id}>v{version.version_number}</option>)}
                </select>
              </label>
              <label>
                {t("studio.version_b")}
                <select aria-label={t("studio.version_b")} value={versionB} disabled={loading || busy || saving} onChange={(event) => setVersionB(event.target.value)}>
                  {versionRows.map((version) => <option key={version.id} value={version.id}>v{version.version_number}</option>)}
                </select>
              </label>
            </div>
            <label>
              {t("studio.input")}
              <textarea aria-label={t("studio.input")} rows={5} value={input} disabled={busy} placeholder={t("studio.input_placeholder")} onChange={(event) => setInput(event.target.value)} />
            </label>
            <div className="studio-actions">
              <button type="button" disabled={!versionA || !input.trim() || loading || busy || saving} onClick={() => startRuns(false)}>
                {running.a && !running.b ? t("studio.running") : t("studio.run_a")}
              </button>
              <button type="button" className="secondary" disabled={!versionA || !versionB || versionA === versionB || !input.trim() || loading || busy || saving} onClick={() => startRuns(true)}>
                {running.a && running.b ? t("studio.running") : t("studio.compare")}
              </button>
            </div>
          </section>

          {!runs.a && !runs.b && !runErrors.a && !runErrors.b && <section className="card studio-empty">{t("studio.no_run")}</section>}
          <div className="studio-run-results">
            {(["a", "b"] as Side[]).map((side) => {
              const run = runs[side];
              if (!run && !runErrors[side]) return null;
              return (
                <div key={side}>
                  {run && (
                    <button type="button" className={`studio-run-result ${traceSide === side ? "active" : ""}`} onClick={() => setTraceSide(side)}>
                      <span className="studio-card-head">
                        <strong>{side.toUpperCase()} · {displayVersion(run.agent_version_id)}</strong>
                        <span className={`badge ${run.status}`}>{run.status}</span>
                      </span>
                      <span className="studio-run-output">{run.output || t("studio.output_empty")}</span>
                      <span className="studio-run-meta">
                        {t("studio.run_meta", { status: run.status, steps: run.step_count, tokens: run.total_tokens, duration: run.duration_ms })}
                      </span>
                    </button>
                  )}
                  {runErrors[side] && <div className="banner err" role="alert">{runErrors[side]}</div>}
                </div>
              );
            })}
          </div>
        </div>

        <div className="studio-column">
          <section className="card">
            <div className="studio-card-head">
              <h3>{t("studio.trace")}</h3>
              <div className="studio-trace-switch">
                {runs.a && <button type="button" className={traceSide === "a" ? "" : "secondary"} onClick={() => setTraceSide("a")}>{t("studio.trace_a")}</button>}
                {runs.b && <button type="button" className={traceSide === "b" ? "" : "secondary"} onClick={() => setTraceSide("b")}>{t("studio.trace_b")}</button>}
              </div>
            </div>
            {activeRun ? (
              <>
                <p className="muted">Run {activeRun.id.slice(0, 8)} · {displayVersion(activeRun.agent_version_id)}</p>
                <TraceTree steps={activeRun.trace} />
              </>
            ) : <div className="studio-empty">{t("studio.no_trace")}</div>}
          </section>
        </div>
      </div>
    </div>
  );
}

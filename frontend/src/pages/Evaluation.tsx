import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useAgentStore } from "../stores/agentStore";
import { Evals, type EvalCase, type EvalRun } from "../api/client";
import EvalHeatmap from "../components/EvalHeatmap";

const SAMPLE_CASES: EvalCase[] = [
  { input: "What is 2 + 2?", expected: "4" },
  { input: "Capital of France?", expected: "Paris" },
];

export default function Evaluation() {
  const { t } = useTranslation();
  const { agents, loadAgents } = useAgentStore();
  const [agentId, setAgentId] = useState("");
  const [datasetName, setDatasetName] = useState("default");
  const [scorer, setScorer] = useState("contains");
  const [threshold, setThreshold] = useState(0.8);
  const [cases, setCases] = useState<EvalCase[]>(SAMPLE_CASES);
  const [evals, setEvals] = useState<EvalRun[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadAgents();
  }, [loadAgents]);

  useEffect(() => {
    if (agents.length && !agentId) setAgentId(agents[0].id);
  }, [agents, agentId]);

  useEffect(() => {
    if (!agentId) {
      setEvals([]);
      return;
    }
    Evals.list(agentId).then(setEvals).catch((e) => setError(String(e)));
  }, [agentId]);

  const addCase = () => setCases((cs) => [...cs, { input: "", expected: "" }]);
  const removeCase = (i: number) => setCases((cs) => cs.filter((_, idx) => idx !== i));
  const updateCase = (i: number, key: keyof EvalCase, val: string) => {
    setCases((cs) => cs.map((c, idx) => (idx === i ? { ...c, [key]: val } : c)));
  };

  const onStart = async () => {
    if (!agentId) return;
    if (cases.some((c) => !c.input.trim() || !c.expected.trim())) {
      setError(t("eval.cases_required"));
      return;
    }
    setRunning(true);
    setError(null);
    try {
      const ev = await Evals.start(agentId, {
        dataset_name: datasetName,
        scorer,
        pass_threshold: threshold,
        cases,
      });
      setEvals((es) => [ev, ...es]);
      // poll
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        const fresh = await Evals.get(agentId, ev.id);
        if (fresh.status === "completed" || fresh.status === "failed") {
          setEvals((es) => es.map((e) => (e.id === ev.id ? fresh : e)));
          break;
        }
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("eval.eval_failed"));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page">
      <h2>{t("eval.title")}</h2>
      {error && <div className="banner err" role="alert">{error}</div>}

      <section className="card">
        <div className="row wrap">
          <label style={{ flex: "1 1 220px" }}>
            {t("eval.agent")}
            <select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
              {agents.length === 0 && <option value="">{t("eval.no_agents")}</option>}
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </label>
          <label style={{ flex: "1 1 200px" }}>
            {t("eval.dataset")}
            <input value={datasetName} onChange={(e) => setDatasetName(e.target.value)} />
          </label>
          <label style={{ flex: "1 1 160px" }}>
            {t("eval.scorer")}
            <select value={scorer} onChange={(e) => setScorer(e.target.value)}>
              <option value="contains">contains</option>
              <option value="exact">exact</option>
              <option value="regex">regex</option>
            </select>
          </label>
          <label style={{ flex: "0 1 140px" }}>
            {t("eval.pass_threshold")}
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={threshold}
              onChange={(e) => setThreshold(parseFloat(e.target.value))}
            />
          </label>
        </div>
      </section>

      <section className="card">
        <div className="row between">
          <h3>{t("eval.test_cases")}</h3>
          <button onClick={addCase} className="secondary">+ {t("eval.add_case")}</button>
        </div>
        <table>
          <thead>
            <tr>
              <th>{t("eval.col_input")}</th>
              <th>{t("eval.col_expected")}</th>
              <th style={{ width: 56 }}></th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c, i) => (
              <tr key={i}>
                <td>
                  <input
                    value={c.input}
                    onChange={(e) => updateCase(i, "input", e.target.value)}
                  />
                </td>
                <td>
                  <input
                    value={c.expected}
                    onChange={(e) => updateCase(i, "expected", e.target.value)}
                  />
                </td>
                <td style={{ textAlign: "right" }}>
                  <button onClick={() => removeCase(i)} className="danger icon-only" title="Remove">
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="form-actions">
          <button
            onClick={onStart}
            disabled={!agentId || running}
            className="lg"
          >
            {running ? t("eval.running") : t("eval.start")}
          </button>
        </div>
      </section>

      {agentId && evals.length > 0 ? (
        <section className="card">
          <EvalHeatmap agentId={agentId} runs={evals} maxRuns={8} />
        </section>
      ) : null}

      <section className="card">
        <h3>{t("eval.past")}</h3>
        {evals.length === 0 ? (
          <div className="empty">
            <div className="empty-icon" aria-hidden="true">📊</div>
            <div className="empty-title">{t("eval.empty")}</div>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>{t("eval.col_dataset")}</th>
                <th>{t("eval.col_scorer")}</th>
                <th>{t("eval.col_status")}</th>
                <th>{t("eval.col_pass_rate")}</th>
                <th>{t("eval.col_started")}</th>
              </tr>
            </thead>
            <tbody>
              {evals.map((e) => (
                <tr key={e.id}>
                  <td>{e.dataset_name}</td>
                  <td>
                    <code>{e.scorer}</code>
                  </td>
                  <td>
                    <span className={`badge ${e.status}`}>{e.status}</span>
                  </td>
                  <td>
                    {t("eval.pass_rate_value", {
                      passed: e.passed,
                      total: e.total,
                      pct: (e.pass_rate * 100).toFixed(0),
                    })}
                  </td>
                  <td className="muted">{new Date(e.started_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
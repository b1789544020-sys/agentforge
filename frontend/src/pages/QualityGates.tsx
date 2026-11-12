import { useEffect, useState } from "react";
import { QualityGates, type AgentVersion, type EvalSuite, type GateRun } from "../api/client";
import { useAgentStore } from "../stores/agentStore";
import { GateQualityEvidence } from "../components/QualityEvidence";

export default function QualityGatePanel({ onOpenRun }: {
  onOpenRun?: (agentId: string, runId: string, versionId: string) => void;
}) {
  const { agents, loadAgents } = useAgentStore();
  const [agentId, setAgentId] = useState("");
  const [versions, setVersions] = useState<AgentVersion[]>([]);
  const [suites, setSuites] = useState<EvalSuite[]>([]);
  const [gates, setGates] = useState<GateRun[]>([]);
  const [suiteId, setSuiteId] = useState("");
  const [versionId, setVersionId] = useState("");
  const [selected, setSelected] = useState<GateRun | null>(null);
  const [suiteName, setSuiteName] = useState("发布回归套件");
  const [caseIds, setCaseIds] = useState("");
  const [threshold, setThreshold] = useState(0.95);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { loadAgents(); }, [loadAgents]);
  useEffect(() => {
    if (!agentId && agents.length) setAgentId(agents[0].id);
  }, [agents, agentId]);
  useEffect(() => {
    if (!agentId) return;
    setError("");
    Promise.all([QualityGates.versions(agentId), QualityGates.suites(agentId)])
      .then(([versionRows, suiteRows]) => {
        setVersions(versionRows);
        setSuites(suiteRows);
        setVersionId((current) => current || versionRows[0]?.id || "");
        setSuiteId((current) => current || suiteRows[0]?.id || "");
      })
      .catch((reason) => setError(String(reason)));
  }, [agentId]);
  useEffect(() => {
    if (!agentId) return;
    QualityGates.list(agentId, suiteId || undefined).then(async (rows) => {
      setGates(rows);
      const currentId = selected?.id;
      const summary = rows.find((row) => row.id === currentId) || rows[0];
      setSelected(summary ? await QualityGates.get(agentId, summary.id) : null);
    }).catch((reason) => setError(String(reason)));
  }, [agentId, suiteId, selected?.id]);

  const refresh = async (gateId: string) => {
    const fresh = await QualityGates.get(agentId, gateId);
    setSelected(fresh);
    setGates((rows) => rows.map((row) => row.id === fresh.id ? fresh : row));
    return fresh;
  };

  const createSuite = async () => {
    if (!agentId || !suiteName.trim()) return;
    const ids = caseIds.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean);
    if (!ids.length) { setError("请至少填写一个评测用例 ID"); return; }
    setBusy(true); setError("");
    try {
      const suite = await QualityGates.createSuite({
        name: suiteName.trim(), agent_id: agentId, case_ids: ids, pass_threshold: threshold,
      });
      setSuites((rows) => [suite, ...rows]); setSuiteId(suite.id); setCaseIds("");
    } catch (reason) { setError(String(reason)); } finally { setBusy(false); }
  };

  const trigger = async () => {
    if (!agentId || !suiteId || !versionId) return;
    setBusy(true); setError("");
    try {
      const gate = await QualityGates.trigger(agentId, versionId, suiteId);
      setGates((rows) => [gate, ...rows]); setSelected(gate);
      let current = gate;
      for (let index = 0; index < 40 && ["pending", "running"].includes(current.status); index += 1) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        current = await refresh(gate.id);
      }
      if (!["pending", "running"].includes(current.status)) {
        // The gate updates AgentVersion.gate_status on the backend.  Refresh
        // the version options as well as the gate detail so the header badge
        // cannot remain "pending" beside an already-passed history row.
        setVersions(await QualityGates.versions(agentId));
      }
    } catch (reason) { setError(String(reason)); } finally { setBusy(false); }
  };

  const selectedVersion = versions.find((row) => row.id === versionId);
  const failedCases = selected?.details.filter((row) => !row.passed) || [];

  return (
    <div className="page">
      <h2>质量门禁</h2>
      {error && <div className="banner err" role="alert">{error}</div>}
      <section className="card">
        <div className="row wrap">
          <label style={{ flex: "1 1 240px" }}>Agent
            <select value={agentId} onChange={(event) => {
              setAgentId(event.target.value); setVersionId(""); setSuiteId(""); setSelected(null);
            }}>
              {!agents.length && <option value="">暂无 Agent</option>}
              {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
            </select>
          </label>
          <label style={{ flex: "1 1 240px" }}>待检版本
            <select value={versionId} onChange={(event) => setVersionId(event.target.value)}>
              {versions.map((version) => <option key={version.id} value={version.id}>
                v{version.version_number} · {version.gate_status}
              </option>)}
            </select>
          </label>
          {selectedVersion && <span className={`badge ${selectedVersion.gate_status}`}>
            gate: {selectedVersion.gate_status}
          </span>}
        </div>
      </section>

      <section className="card">
        <h3>新建回归套件</h3>
        <div className="row wrap">
          <label style={{ flex: "1 1 180px" }}>名称
            <input value={suiteName} onChange={(event) => setSuiteName(event.target.value)} />
          </label>
          <label style={{ flex: "0 1 160px" }}>通过率阈值
            <input type="number" min="0" max="1" step="0.05" value={threshold}
              onChange={(event) => setThreshold(Number(event.target.value))} />
          </label>
        </div>
        <label>评测用例 ID（逗号或换行分隔）
          <textarea value={caseIds} onChange={(event) => setCaseIds(event.target.value)}
            placeholder="粘贴固定 EvalCase ID，多个用例共同组成回归基线" />
        </label>
        <div className="form-actions"><button onClick={createSuite} disabled={busy || !agentId}>创建套件</button></div>
      </section>

      <div className="gate-layout">
        <section className="card">
          <h3>套件</h3>
          {!suites.length && <div className="empty">尚无套件</div>}
          {suites.map((suite) => (
            <button key={suite.id} className={`ghost suite-row ${suiteId === suite.id ? "active" : ""}`}
              onClick={() => setSuiteId(suite.id)}>
              <span>{suite.name}</span>
              <small>{suite.case_ids.length} 用例 · ≥ {(suite.pass_threshold * 100).toFixed(0)}%</small>
            </button>
          ))}
          <div className="form-actions">
            <button onClick={trigger} disabled={busy || !suiteId || !versionId}>
              {busy ? "执行中…" : "手动触发门禁"}
            </button>
          </div>
        </section>

        <section className="card">
          <h3>历史门禁</h3>
          {!gates.length && <div className="empty">尚无门禁记录</div>}
          {!!gates.length && <table><thead><tr><th>时间</th><th>触发</th><th>结果</th><th>通过率</th></tr></thead>
            <tbody>{gates.map((gate) => <tr key={gate.id} onClick={() => void refresh(gate.id)} className="clickable">
              <td>{new Date(gate.created_at).toLocaleString()}</td><td>{gate.trigger_source}</td>
              <td><span className={`badge ${gate.status}`}>{gate.status}</span></td>
              <td>{(gate.pass_rate * 100).toFixed(0)}% / {(gate.threshold * 100).toFixed(0)}%</td>
            </tr>)}</tbody></table>}
        </section>
      </div>

      {selected && <section className="card">
        <h3>门禁明细 <span className={`badge ${selected.status}`}>{selected.status}</span></h3>
        {selected.error && <div className="banner err">{selected.error}</div>}
        <h4>四层质量证据</h4>
        <GateQualityEvidence gate={selected} onOpenRun={onOpenRun ? (runId, versionId) =>
          onOpenRun(agentId, runId, versionId) : undefined} />
        <h4>失败用例（{failedCases.length}）</h4>
        {!failedCases.length && <p className="muted">没有失败用例</p>}
        {failedCases.map((row) => <div key={row.case_id} className="gate-case">
          <strong>{row.input}</strong><code>{row.case_id}</code>
          <p>期望：{row.expected || "—"}</p><p>实际：{row.output || row.error || "—"}</p>
        </div>)}
        <h4>与上一版本对比</h4>
        {!selected.diff.length && <p className="muted">首个门禁，无可比较基线</p>}
        {selected.diff.map((row) => <details key={row.case_id} className={`diff-row diff-${row.change}`}>
          <summary><span className={`badge ${row.change}`}>{row.change}</span> <code>{row.case_id}</code>
            {row.comparison_status && <span className={`badge ${row.comparison_status === "comparable" ? "passed" : "pending"}`}>{row.comparison_status}</span>}
          </summary>
          {!!row.layer_changes?.length && <div className="row wrap">{row.layer_changes.map((layer) =>
            <span className={`badge ${layer.change}`} key={layer.layer}>{layer.layer}: {layer.change}</span>)}</div>}
          {!!row.changed_conditions?.length && <p className="quality-warning">比较条件已变化：{row.changed_conditions.join("、")}</p>}
          {row.answer_diff ? <pre>{row.answer_diff}</pre> : <p className="muted">答案无文本变化或没有两侧结果</p>}
        </details>)}
      </section>}
    </div>
  );
}

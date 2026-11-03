import { useEffect, useState } from "react";
import { Deliveries, QualityGates, Templates, type AgentTemplate, type AgentVersion, type Deployment } from "../api/client";
import { useAgentStore } from "../stores/agentStore";

export default function DeliveryCenter() {
  const { agents, loadAgents } = useAgentStore();
  const [agentId, setAgentId] = useState("");
  const [versions, setVersions] = useState<AgentVersion[]>([]);
  const [versionId, setVersionId] = useState("");
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [templates, setTemplates] = useState<AgentTemplate[]>([]);
  const [name, setName] = useState("生产助手");
  const [secret, setSecret] = useState<Deployment | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => { loadAgents(); }, [loadAgents]);
  useEffect(() => { if (!agentId && agents.length) setAgentId(agents[0].id); }, [agents, agentId]);
  useEffect(() => {
    if (!agentId) return;
    QualityGates.versions(agentId).then((rows) => {
      setVersions(rows); setVersionId((current) => current || rows[0]?.id || "");
    }).catch((reason) => setError(String(reason)));
  }, [agentId]);
  const reload = async () => {
    const [deploymentRows, templateRows] = await Promise.all([Deliveries.list(), Templates.list()]);
    setDeployments(deploymentRows); setTemplates(templateRows);
  };
  useEffect(() => { reload().catch((reason) => setError(String(reason))); }, []);

  const publish = async () => {
    if (!versionId) return;
    try {
      const row = await Deliveries.publish(versionId, name);
      setSecret(row); setMessage("发布成功，token 仅此一次完整显示"); await reload();
    } catch (reason) { setError(String(reason)); }
  };
  const action = async (id: string, operation: "start" | "stop" | "rollback" | "revoke") => {
    try { await Deliveries.action(id, operation); await reload(); }
    catch (reason) { setError(String(reason)); }
  };
  const rotate = async (id: string) => {
    try { const row = await Deliveries.rotate(id); setSecret(row); await reload(); }
    catch (reason) { setError(String(reason)); }
  };
  const instantiate = async (id: string) => {
    try {
      const result = await Templates.instantiate(id);
      setMessage(`已从模板创建 ${result.agent.name}`); await loadAgents();
    } catch (reason) { setError(String(reason)); }
  };
  const selected = versions.find((row) => row.id === versionId);
  const canPublish = selected && ["passed", "bypassed"].includes(selected.gate_status);

  return (
    <div className="page"><h2>一键交付与模板</h2>
      {error && <div className="banner err">{error}</div>}{message && <div className="banner ok">{message}</div>}
      <section className="card"><h3>发布 Agent 版本</h3><div className="row wrap">
        <label>Agent<select value={agentId} onChange={(event) => { setAgentId(event.target.value); setVersionId(""); }}>
          {agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}
        </select></label>
        <label>版本<select value={versionId} onChange={(event) => setVersionId(event.target.value)}>
          {versions.map((version) => <option value={version.id} key={version.id}>v{version.version_number} · {version.gate_status}</option>)}
        </select></label>
        <label>交付名称<input value={name} onChange={(event) => setName(event.target.value)} /></label>
        <button onClick={publish} disabled={!canPublish} title={!canPublish ? "版本必须先通过门禁" : ""}>发布</button>
      </div>{selected && !canPublish && <p className="muted">当前版本 gate_status={selected.gate_status}，发布入口已锁定。</p>}</section>
      {secret?.token && <section className="card"><h3>独立交付凭据（请立即保存）</h3>
        <p><code>{secret.token}</code></p><p>REST：<code>{secret.api_url}</code></p>
        <p>独立页面：<a href={secret.page_url} target="_blank" rel="noreferrer">{secret.page_url}</a></p>
        <p>iframe：<code>{`<iframe src="${location.origin}${secret.embed_url}" title="AI 助手"></iframe>`}</code></p>
      </section>}
      <section className="card"><h3>交付产物</h3>
        <table><thead><tr><th>名称</th><th>状态</th><th>Token</th><th>历史</th><th>操作</th></tr></thead>
          <tbody>{deployments.map((row) => <tr key={row.id}><td>{row.name}</td><td>{row.status}{row.token_revoked && " / revoked"}</td>
            <td>{row.token_prefix}…</td><td>{row.version_history.length}</td><td className="row wrap">
              <button className="secondary" onClick={() => action(row.id, row.status === "active" ? "stop" : "start")}>{row.status === "active" ? "停用" : "启用"}</button>
              <button className="secondary" disabled={row.version_history.length < 2} onClick={() => action(row.id, "rollback")}>回滚</button>
              <button className="secondary" onClick={() => rotate(row.id)}>轮换 token</button>
              <button className="danger" onClick={() => action(row.id, "revoke")}>吊销</button>
            </td></tr>)}</tbody></table>
      </section>
      <section className="card"><h3>场景模板（{templates.length}）</h3>
        <div className="metric-grid">{templates.map((template) => <div key={template.id}>
          <strong>{template.name}</strong><span>{template.category} · v{template.version_number}{template.builtin && " · 内置"}</span>
          <p className="muted">{template.description}</p><button className="secondary" onClick={() => instantiate(template.id)}>从模板创建</button>
        </div>)}</div>
      </section>
    </div>
  );
}

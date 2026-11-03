import { useEffect, useState } from "react";
import { Prompts, type PromptTemplate, type PromptComparisonSide } from "../api/client";
import { useAgentStore } from "../stores/agentStore";

export default function PromptTemplates() {
  const { agents, loadAgents } = useAgentStore();
  const [agentId, setAgentId] = useState("");
  const [templates, setTemplates] = useState<PromptTemplate[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [name, setName] = useState("");
  const [content, setContent] = useState("You are a {{ role }} assistant. Answer {{ input }}.");
  const [variables, setVariables] = useState('{"role":"helpful"}');
  const [caseInput, setCaseInput] = useState("Say hello");
  const [expected, setExpected] = useState("");
  const [comparison, setComparison] = useState<{
    version_a: PromptComparisonSide;
    version_b: PromptComparisonSide;
    delta: { average_score: number; pass_rate: number };
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selected = templates.find((item) => item.id === selectedId);

  useEffect(() => { loadAgents(); }, [loadAgents]);
  useEffect(() => {
    if (agents.length && !agentId) setAgentId(agents[0].id);
  }, [agents, agentId]);
  useEffect(() => {
    if (!agentId) return;
    Prompts.list(agentId).then((rows) => {
      setTemplates(rows);
      setSelectedId((current) => current || rows[0]?.id || "");
    }).catch((e) => setError(String(e)));
  }, [agentId]);

  const parsedVariables = (): Record<string, unknown> => {
    const value = JSON.parse(variables);
    if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("Variables must be a JSON object");
    return value;
  };
  const refresh = async (id = selectedId) => {
    const rows = await Prompts.list(agentId);
    setTemplates(rows);
    setSelectedId(id);
  };
  const run = async (action: () => Promise<void>) => {
    setError(null);
    try { await action(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return <div className="page">
    <h2>Prompt 模板与版本</h2>
    {error && <div className="banner err" role="alert">{error}</div>}
    <section className="card">
      <div className="row wrap">
        <label>Agent
          <select value={agentId} onChange={(e) => { setAgentId(e.target.value); setSelectedId(""); }}>
            {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
          </select>
        </label>
        <label>模板名<input value={name} onChange={(e) => setName(e.target.value)} placeholder="writer" /></label>
      </div>
      <label>Jinja 模板<textarea rows={4} value={content} onChange={(e) => setContent(e.target.value)} /></label>
      <label>变量（JSON）<textarea rows={2} value={variables} onChange={(e) => setVariables(e.target.value)} /></label>
      <div className="form-actions">
        <button disabled={!agentId || !name.trim() || !content.trim()} onClick={() => run(async () => {
          const created = await Prompts.create(agentId, { name, template: content });
          await refresh(created.id);
        })}>创建模板</button>
        {selected && <button className="secondary" onClick={() => run(async () => {
          await Prompts.addVersion(agentId, selected.id, content);
          await refresh();
        })}>保存为新版本</button>}
      </div>
    </section>

    {selected && <section className="card">
      <h3>{selected.name} · 最新 v{selected.latest_version}</h3>
      <div className="row wrap">
        {selected.versions.map((version) => <span className="badge" key={version.id}>v{version.version}</span>)}
      </div>
      <div className="form-actions">
        <button className="secondary" onClick={() => run(async () => {
          await Prompts.bind(agentId, selected.id, selected.latest_version, parsedVariables());
        })}>绑定最新版本到 Agent</button>
      </div>
      <div className="grid-2">
        <label>对比输入<input value={caseInput} onChange={(e) => setCaseInput(e.target.value)} /></label>
        <label>期望答案（可空）<input value={expected} onChange={(e) => setExpected(e.target.value)} /></label>
      </div>
      <button disabled={selected.latest_version < 2} onClick={() => run(async () => {
        setComparison(await Prompts.compare(agentId, selected.id, {
          version_a: selected.latest_version - 1,
          version_b: selected.latest_version,
          cases: [{ input: caseInput, expected }],
          scorer_name: "answer",
          variables: parsedVariables(),
        }));
      })}>对比最近两个版本</button>
      {comparison && <div className="row wrap" style={{ marginTop: 12 }}>
        <span>v{comparison.version_a.version}: {(comparison.version_a.average_score * 100).toFixed(1)}%</span>
        <span>v{comparison.version_b.version}: {(comparison.version_b.average_score * 100).toFixed(1)}%</span>
        <strong>Δ {(comparison.delta.average_score * 100).toFixed(1)}%</strong>
      </div>}
    </section>}
  </div>;
}

import { useEffect, useState } from "react";
import { Knowledge, type KnowledgeBase, type KnowledgeDocument, type KnowledgeHit } from "../api/client";
import { useAgentStore } from "../stores/agentStore";

export default function KnowledgePage() {
  const { agents, loadAgents } = useAgentStore();
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [kbId, setKbId] = useState("");
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [agentId, setAgentId] = useState("");
  const [name, setName] = useState("企业知识库");
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<KnowledgeHit[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => { loadAgents(); }, [loadAgents]);
  useEffect(() => { if (!agentId && agents.length) setAgentId(agents[0].id); }, [agents, agentId]);
  useEffect(() => {
    Knowledge.list().then((rows) => { setKbs(rows); setKbId((current) => current || rows[0]?.id || ""); })
      .catch((reason) => setError(String(reason)));
  }, []);
  useEffect(() => {
    if (!kbId) { setDocuments([]); return; }
    Knowledge.documents(kbId).then(setDocuments).catch((reason) => setError(String(reason)));
  }, [kbId]);

  const create = async () => {
    try { const row = await Knowledge.create(name); setKbs((rows) => [row, ...rows]); setKbId(row.id); }
    catch (reason) { setError(String(reason)); }
  };
  const upload = async (file?: File) => {
    if (!file || !kbId) return;
    try {
      const doc = await Knowledge.upload(kbId, file);
      setMessage(`${doc.filename} 已切片 ${doc.chunks} 段`);
      setDocuments(await Knowledge.documents(kbId));
    } catch (reason) { setError(String(reason)); }
  };
  const bind = async () => {
    if (!agentId || !kbId) return;
    try { await Knowledge.bind(agentId, kbId); setMessage("知识库已绑定 Agent"); }
    catch (reason) { setError(String(reason)); }
  };
  const search = async () => {
    if (!agentId || !kbId || !query.trim()) return;
    try { setHits(await Knowledge.search(kbId, agentId, query.trim())); }
    catch (reason) { setError(String(reason)); }
  };

  return (
    <div className="page"><h2>私有知识库</h2>
      {error && <div className="banner err">{error}</div>}{message && <div className="banner ok">{message}</div>}
      <section className="card"><h3>创建与选择</h3><div className="row wrap">
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="知识库名称" />
        <button onClick={create}>创建知识库</button>
        <select value={kbId} onChange={(event) => setKbId(event.target.value)}>
          {!kbs.length && <option value="">暂无知识库</option>}
          {kbs.map((kb) => <option key={kb.id} value={kb.id}>{kb.name}</option>)}
        </select>
      </div></section>
      <section className="card"><h3>上传文档</h3>
        <input type="file" accept=".pdf,.docx,.txt,.md,.markdown" onChange={(event) => upload(event.target.files?.[0])} disabled={!kbId} />
        <p className="muted">支持 PDF / DOCX / UTF-8 TXT / Markdown；按中文标题、段落和完整句子切片。</p>
        <table><thead><tr><th>文件</th><th>状态</th><th>切片数</th><th>错误</th></tr></thead>
          <tbody>{documents.map((doc) => <tr key={doc.id}><td>{doc.filename}</td><td>{doc.status}</td><td>{doc.chunks}</td><td>{doc.error}</td></tr>)}</tbody>
        </table>
      </section>
      <section className="card"><h3>绑定与检索测试</h3><div className="row wrap">
        <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
          {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
        </select><button className="secondary" onClick={bind} disabled={!kbId || !agentId}>绑定到 Agent</button>
      </div><div className="row wrap">
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入中文问题，如：退款审核需要多久？" />
        <button onClick={search} disabled={!kbId || !agentId || !query.trim()}>检索</button>
      </div>{hits.map((hit, index) => <div className="gate-case" key={index}>
        <strong>[{index + 1}] {hit.document}{hit.section ? `#${hit.section}` : ""}</strong>
        <span className="muted"> · {hit.score.toFixed(3)}</span><p>{hit.content}</p>
      </div>)}</section>
    </div>
  );
}

import { useEffect, useState } from "react";
import { Operations, type AlertEvent, type AlertRule, type CostResponse, type FeedbackItem, type FeedbackResolutionItem, type MetricsResponse } from "../api/client";
import { useAgentStore } from "../stores/agentStore";

export default function OperationsPage({ permissions = ["*"] }: { permissions?: string[] }) {
  const { agents, loadAgents } = useAgentStore();
  const [agentId, setAgentId] = useState("");
  const [teamId, setTeamId] = useState("");
  const [model, setModel] = useState("");
  const [group, setGroup] = useState("day");
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [cost, setCost] = useState<CostResponse | null>(null);
  const [feedback, setFeedback] = useState<FeedbackItem[]>([]);
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [corrections, setCorrections] = useState<Record<string, string>>({});
  const [suiteIds, setSuiteIds] = useState<Record<string, string>>({});
  const [reviewGateIds, setReviewGateIds] = useState<Record<string, string>>({});
  const [reviewNotes, setReviewNotes] = useState<Record<string, string>>({});
  const [resolutionHistory, setResolutionHistory] = useState<Record<string, FeedbackResolutionItem[]>>({});
  const [ruleName, setRuleName] = useState("运行失败率告警");
  const [ruleKind, setRuleKind] = useState("failure_rate");
  const [threshold, setThreshold] = useState(0.2);
  const [error, setError] = useState("");
  const globalAdmin = permissions.includes("*");
  const canResolve = globalAdmin || permissions.includes("approval:manage");

  useEffect(() => { loadAgents(); }, [loadAgents]);
  useEffect(() => {
    const params: Record<string, string> = {};
    if (agentId) params.agent_id = agentId;
    if (teamId) params.team_id = teamId;
    if (model) params.model = model;
    Promise.all([
      Operations.metrics({ ...params, group }), Operations.cost(params), Operations.feedback(),
      Operations.rules(), Operations.events(),
    ]).then(async ([metricData, costData, feedbackData, ruleData, eventData]) => {
      setMetrics(metricData); setCost(costData); setFeedback(feedbackData); setRules(ruleData); setEvents(eventData);
      const histories = await Promise.all(feedbackData.map(async (row) => [row.id, await Operations.resolutions(row.id)] as const));
      setResolutionHistory(Object.fromEntries(histories));
      setError("");
    }).catch((reason) => setError(String(reason)));
  }, [agentId, teamId, model, group]);

  const convert = async (id: string) => {
    const answer = (corrections[id] ?? feedback.find((row) => row.id === id)?.expected_answer ?? "").trim();
    if (!answer) { setError("请先填写人工修正后的期望答案"); return; }
    try {
      await Operations.convertFeedback(id, answer, suiteIds[id]?.trim() || undefined);
      setFeedback(await Operations.feedback());
    } catch (reason) { setError(String(reason)); }
  };
  const resolve = async (row: FeedbackItem, verdict: FeedbackResolutionItem["verdict"]) => {
    const gateRunId = reviewGateIds[row.id]?.trim();
    const note = reviewNotes[row.id]?.trim();
    if (!gateRunId || !note || !row.eval_case_id) {
      setError("人工复核需要 GateRun ID、复核理由和已关联的 EvalCase"); return;
    }
    try {
      await Operations.resolveFeedback(row.id, {
        gate_run_id: gateRunId, case_id: row.eval_case_id, verdict, note,
      });
      const history = await Operations.resolutions(row.id);
      setResolutionHistory((current) => ({ ...current, [row.id]: history }));
      setError("");
    } catch (reason) { setError(String(reason)); }
  };
  const createRule = async () => {
    try {
      await Operations.createRule({ name: ruleName, kind: ruleKind, threshold });
      setRules(await Operations.rules());
    } catch (reason) { setError(String(reason)); }
  };
  const evaluate = async () => {
    try { await Operations.evaluate(); setEvents(await Operations.events()); }
    catch (reason) { setError(String(reason)); }
  };
  const acknowledge = async (id: string) => {
    try { await Operations.acknowledge(id); setEvents(await Operations.events()); }
    catch (reason) { setError(String(reason)); }
  };
  const maxRequests = Math.max(1, ...(metrics?.trends.map((row) => row.requests) || [1]));

  return (
    <div className="page">
      <h2>运营与成本</h2>
      {error && <div className="banner err" role="alert">{error}</div>}
      <section className="card"><div className="row wrap">
        <label>Agent<select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
          <option value="">全部 Agent</option>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
        </select></label>
        <label>Team ID<input value={teamId} onChange={(event) => setTeamId(event.target.value)} placeholder="全部可见团队" /></label>
        <label>模型<input value={model} onChange={(event) => setModel(event.target.value)} placeholder="全部模型" /></label>
        <label>时间粒度<select value={group} onChange={(event) => setGroup(event.target.value)}>
          <option value="hour">小时</option><option value="day">天</option><option value="week">周</option>
        </select></label>
      </div></section>
      {metrics && <section className="card"><h3>运行指标</h3>
        <div className="metric-grid">
          <div><strong>{metrics.summary.requests}</strong><span>请求</span></div>
          <div><strong>{(metrics.summary.failure_rate * 100).toFixed(1)}%</strong><span>失败率</span></div>
          <div><strong>{metrics.summary.rejected}</strong><span>拒绝</span></div>
          <div><strong>{metrics.summary.total_tokens.toLocaleString()}</strong><span>Tokens</span></div>
          <div><strong>{metrics.summary.p95_duration_ms.toFixed(0)}ms</strong><span>p95</span></div>
        </div>
        <div className="trend">{metrics.trends.map((row) => <div key={row.bucket} className="trend-item" title={`${row.bucket}: ${row.requests} requests`}>
          <div className="trend-bar" style={{ height: `${Math.max(4, row.requests / maxRequests * 100)}%` }} />
          <small>{row.bucket.slice(0, 10)}</small>
        </div>)}</div>
      </section>}
      {cost && <section className="card"><h3>成本中心</h3>
        <p><strong>{cost.total.toFixed(6)} {cost.currency}</strong> <span className="muted">未计价 tokens: {cost.unpriced_tokens}</span></p>
        <div className="gate-layout"><div><h4>TOP Agent</h4>{cost.top_agents.map((row) =>
          <p key={row.agent_id}><code>{row.agent_id}</code> · {row.cost.toFixed(6)}</p>)}</div>
        <div><h4>模型分布</h4>{cost.models.map((row) => <p key={row.model}>{row.model} · {row.cost.toFixed(6)}</p>)}</div></div>
      </section>}
      <section className="card"><h3>坏样本回收（{feedback.length}）</h3>
        {!feedback.length && <p className="muted">暂无负面反馈</p>}
        {feedback.map((row) => <div className="gate-case" key={row.id}>
          <p><strong>输入：</strong>{row.context_snapshot.input}</p><p><strong>输出：</strong>{row.context_snapshot.output}</p>
          <small className="muted">精确来源版本：{row.agent_version_id || "legacy"} {row.eval_case_id && `· EvalCase ${row.eval_case_id}`}</small>
          {!row.eval_case_id && <div className="form">
            <textarea value={corrections[row.id] ?? row.expected_answer ?? ""} placeholder="人工修正后的期望答案"
              onChange={(event) => setCorrections((values) => ({ ...values, [row.id]: event.target.value }))} />
            <input value={suiteIds[row.id] || ""} placeholder="加入 Suite ID（可选）"
              onChange={(event) => setSuiteIds((values) => ({ ...values, [row.id]: event.target.value }))} />
            <button onClick={() => convert(row.id)}>转成 EvalCase 并回归</button>
          </div>}
          {!!resolutionHistory[row.id]?.length && <div className="quality-sources"><h4>追加式人工复核</h4>
            {resolutionHistory[row.id].map((item) => <div className="quality-assertion" key={item.id}>
              <span className={`badge ${item.verdict === "confirmed_fixed" ? "passed" : "pending"}`}>{item.verdict}</span>
              <span>{item.note}</span><small>{item.gate_run_id} · {new Date(item.created_at).toLocaleString()}</small>
            </div>)}
          </div>}
          {row.eval_case_id && canResolve && <div className="form quality-review">
            <input aria-label={`复核 GateRun ${row.id}`} value={reviewGateIds[row.id] || ""} placeholder="用于复核的 GateRun ID"
              onChange={(event) => setReviewGateIds((values) => ({ ...values, [row.id]: event.target.value }))} />
            <textarea aria-label={`复核理由 ${row.id}`} value={reviewNotes[row.id] || ""} placeholder="人工核对原 Run、当前 Run 和来源后填写理由"
              onChange={(event) => setReviewNotes((values) => ({ ...values, [row.id]: event.target.value }))} />
            <div className="form-actions"><button className="secondary" onClick={() => resolve(row, "needs_review")}>保留待复核</button>
              <button onClick={() => resolve(row, "confirmed_fixed")}>人工确认已修复</button></div>
          </div>}
        </div>)}
      </section>
      <section className="card"><h3>告警规则与站内通知</h3>
        {globalAdmin && <div className="row wrap">
          <input value={ruleName} onChange={(event) => setRuleName(event.target.value)} placeholder="规则名称" />
          <select value={ruleKind} onChange={(event) => setRuleKind(event.target.value)}>
            <option value="failure_rate">失败率超过阈值</option><option value="daily_cost">单日成本超限</option>
            <option value="error_spike">错误率突增</option><option value="gate_failures">连续门禁失败</option>
          </select>
          <input type="number" min="0.000001" step="0.05" value={threshold} onChange={(event) => setThreshold(Number(event.target.value))} />
          <button onClick={createRule}>创建规则</button><button className="secondary" onClick={evaluate}>立即评估</button>
        </div>}
        <div className="row wrap">{rules.map((rule) => <span className="badge" key={rule.id}>
          {rule.name} · {rule.kind} &gt; {rule.threshold}</span>)}</div>
        <table><thead><tr><th>时间</th><th>通知</th><th>状态</th><th /></tr></thead>
          <tbody>{events.map((event) => <tr key={event.id}>
            <td>{new Date(event.created_at).toLocaleString()}</td><td>{event.message}</td><td>{event.status}</td>
            <td>{event.status === "open" && globalAdmin && <button className="secondary" onClick={() => acknowledge(event.id)}>确认</button>}</td>
          </tr>)}</tbody></table>
      </section>
    </div>
  );
}

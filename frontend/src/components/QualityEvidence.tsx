import type { GateRun, RunDetail } from "../api/client";
import type { QualityLayer, QualityLayerStatus } from "../api/quality";

const LAYERS: { id: QualityLayer; label: string }[] = [
  { id: "output", label: "输出" }, { id: "tool", label: "工具" },
  { id: "retrieval", label: "检索" }, { id: "system", label: "系统" },
];

function LayerBadge({ layer, status }: { layer: QualityLayer; status: QualityLayerStatus }) {
  const label = LAYERS.find((item) => item.id === layer)?.label || layer;
  return <span className={`badge ${status}`} data-layer={layer}>{label} · {status}</span>;
}

export function RunQualityEvidence({ run }: { run: RunDetail }) {
  if (run.quality_record_version == null) {
    return <p className="muted quality-legacy">这是旧 Run：保留原文和 Trace，没有四层质量记录。</p>;
  }
  if (run.quality_record_version !== 1) {
    return <p className="banner info">不支持的新质量记录版本；仍展示旧输出与 Trace。</p>;
  }
  const answer = run.answer_output;
  const tools = run.tool_output;
  const retrieval = run.retrieval_output;
  const execution = run.execution_output;
  const toolErrors = tools?.results.filter((item) => item.status !== "success").length || 0;
  return <div className="quality-evidence" data-testid="run-quality-evidence">
    <div className="quality-layer-grid">
      <div><LayerBadge layer="output" status="not_evaluated" />
        <small>{answer?.answer_status || "missing"} · {answer?.evidence.length || 0} 条引用</small></div>
      <div><LayerBadge layer="tool" status="not_evaluated" />
        <small>{tools?.calls.length || 0} 次调用 · {toolErrors} 个错误</small></div>
      <div><LayerBadge layer="retrieval" status="not_evaluated" />
        <small>{retrieval?.queries.length || 0} 次查询 · {retrieval?.sources.length || 0} 个来源</small></div>
      <div><LayerBadge layer="system" status="not_evaluated" />
        <small>{execution?.result.status || "missing"} · {execution?.result.record_complete ? "记录完整" : "记录不完整"}</small></div>
    </div>
    {execution?.error && <div className="banner err">{execution.error.category}: {execution.error.message}</div>}
    {!!retrieval?.sources.length && <div className="quality-sources"><h4>实际注入的来源</h4>
      {retrieval.sources.map((source, index) => <details key={source.source_id}>
        <summary>[{index + 1}] {source.locator.filename}{source.locator.section ? `#${source.locator.section}` : ""}</summary>
        <p>{source.source_excerpt}</p><code>{source.chunk_sha256.slice(0, 16)}…</code>
      </details>)}
    </div>}
  </div>;
}

export function GateQualityEvidence({ gate, onOpenRun }: {
  gate: GateRun;
  onOpenRun?: (runId: string, versionId: string) => void;
}) {
  if (gate.quality_record_version == null) {
    return <p className="muted quality-legacy">旧门禁只有总分和文本 diff，没有四层证据；这不表示四层全部通过。</p>;
  }
  return <div className="quality-evidence" data-testid="gate-quality-evidence">
    {gate.conditions && <div className="quality-conditions">
      固定条件：{gate.conditions.execution.provider} / {gate.conditions.execution.model} ·
      每案 {gate.conditions.execution.run_count} 次 · {gate.conditions.suite.case_ids.length} 个冻结用例
    </div>}
    {!gate.evidence?.length && <p className="muted">终态前或旧记录暂无可展示的分层证据。</p>}
    {gate.evidence?.map((item) => {
      const detail = gate.details.find((row) => row.case_id === item.case_id);
      return <details key={item.evidence_id} className="quality-case" open={!item.passed}>
        <summary><strong>{detail?.input || item.case_id}</strong> <span className={`badge ${item.passed ? "passed" : "failed"}`}>{item.passed ? "passed" : "failed"}</span></summary>
        <div className="quality-layer-grid four">
          {LAYERS.map(({ id }) => <div key={id}><LayerBadge layer={id} status={item.layers[id].status} />
            <small>{item.layers[id].assertions.length} 项断言</small></div>)}
        </div>
        {LAYERS.flatMap(({ id }) => item.layers[id].assertions.map((assertion) => <div className="quality-assertion" key={`${id}-${assertion.assertion_id}`}>
          <code>{assertion.kind}</code><span>{assertion.reason}</span><small>{assertion.subject.pointer}</small>
        </div>))}
        <div className="row wrap quality-run-link"><code>Run {item.run_id}</code>
          {onOpenRun && <button className="secondary" onClick={(event) => {
            event.preventDefault(); onOpenRun(item.run_id, item.agent_version_id);
          }}>打开确切 Run 与版本</button>}
        </div>
      </details>;
    })}
  </div>;
}

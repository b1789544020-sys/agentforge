/** Versioned quality evidence contracts. Run facts and Gate judgments stay separate. */
export type QualityLayer = "output" | "tool" | "retrieval" | "system";
export type QualityLayerStatus =
  | "passed"
  | "failed"
  | "not_evaluated"
  | "not_available"
  | "error";

export interface QualityError {
  category:
    | "provider"
    | "timeout"
    | "permission"
    | "budget"
    | "cancelled"
    | "tool"
    | "retrieval"
    | "internal";
  message: string;
  retryable: boolean;
  step_id?: string | null;
}

export interface AnswerEvidence {
  evidence_id: string;
  source_id: string;
  citation_text: string;
  answer_excerpt: string;
  source_excerpt: string;
  support_status: "not_checked";
}

export interface AnswerOutput {
  answer: string | null;
  answer_status:
    | "answered"
    | "insufficient_evidence"
    | "refused"
    | "not_produced"
    | "unknown";
  evidence: AnswerEvidence[];
}

export interface ToolCall {
  call_id: string;
  call_step_id: string | null;
  name: string;
  arguments: unknown;
  risk: string;
  requested_at: string;
  approval_id: string | null;
  definition_version: string | null;
}

export interface ToolResult {
  call_id: string;
  result_step_id: string | null;
  status: "success" | "error" | "cancelled" | "timeout" | "denied";
  output: unknown;
  error: QualityError | null;
  duration_ms: number;
  finished_at: string;
}

export interface ToolOutput {
  calls: ToolCall[];
  results: ToolResult[];
}

export interface RetrievalCandidate {
  source_id: string;
  rank: number;
  score: number;
  injected: boolean;
}

export interface RetrievalQuery {
  query_id: string;
  text: string;
  knowledge_base_ids: string[];
  embedding: { provider: string; model: string; dimensions: number };
  index: { snapshot_id: string; algorithm: string; corpus_manifest_sha256: string };
  top_k: number;
  filters: Record<string, unknown>;
  candidates: RetrievalCandidate[];
  duration_ms: number;
  status: "completed" | "error" | "cancelled";
  error: QualityError | null;
}

export interface RetrievalSource {
  source_id: string;
  knowledge_base_id: string;
  document_id: string;
  chunk_id: string;
  document_sha256: string;
  chunk_sha256: string;
  locator: { filename: string; section: string | null };
  source_excerpt: string;
  excerpt_truncated: boolean;
}

export interface RetrievalOutput {
  queries: RetrievalQuery[];
  sources: RetrievalSource[];
}

export interface ExecutionEvent {
  event_id: string;
  step_id: string | null;
  sequence: number;
  kind: string;
  status: string;
  parent_event_id: string | null;
}

export interface ExecutionMetrics {
  duration_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  step_count: number;
  tool_call_count: number;
  retrieval_query_count: number;
}

export interface ExecutionResult {
  status: string;
  started_at: string;
  finished_at: string | null;
  termination_reason: string | null;
  record_complete: boolean;
}

export interface ExecutionOutput {
  events: ExecutionEvent[];
  metrics: ExecutionMetrics;
  error: QualityError | null;
  result: ExecutionResult;
}

export interface RunObservationsV1 {
  quality_record_version: 1;
  answer_output: AnswerOutput | null;
  tool_output: ToolOutput | null;
  retrieval_output: RetrievalOutput | null;
  execution_output: ExecutionOutput | null;
}

export interface RunRecordV1 extends RunObservationsV1 {
  id: string;
  agent_id: string;
  agent_version_id: string | null;
  input: string;
  output: string | null;
  status: string;
}

/** Detail responses also accept legacy null and unsupported future integer versions. */
export interface RunQualityFields {
  quality_record_version?: number | null;
  answer_output?: AnswerOutput | null;
  tool_output?: ToolOutput | null;
  retrieval_output?: RetrievalOutput | null;
  execution_output?: ExecutionOutput | null;
}

export interface AssertionSubject {
  run_id: string;
  pointer: string;
  evidence_id?: string | null;
  query_id?: string | null;
  call_id?: string | null;
  source_id?: string | null;
}

export interface GateAssertion {
  assertion_id: string;
  kind: string;
  status: QualityLayerStatus;
  score: number | null;
  reason: string;
  subject: AssertionSubject;
  rule_version?: string | null;
  details?: Record<string, unknown>;
}

export interface GateLayer {
  status: QualityLayerStatus;
  assertions: GateAssertion[];
}

export interface GateCaseLayers {
  output: GateLayer;
  tool: GateLayer;
  retrieval: GateLayer;
  system: GateLayer;
}

export interface GateCaseEvidence {
  evidence_id: string;
  case_id: string;
  result_case_id: string | null;
  attempt: number;
  run_id: string;
  agent_version_id: string;
  passed: boolean;
  failed_layers: QualityLayer[];
  layers: GateCaseLayers;
  error: QualityError | null;
}

export interface GateCaseSnapshot {
  case_id: string;
  input: string;
  expected: unknown;
  metadata: Record<string, unknown>;
  input_sha256: string;
  expected_sha256: string;
  metadata_sha256: string;
}

export interface GateConditions {
  suite: {
    suite_id: string;
    case_ids: string[];
    case_snapshots: GateCaseSnapshot[];
    scorer: {
      name: string;
      version: string;
      config: Record<string, unknown>;
      config_sha256: string;
    };
    case_pass_threshold: number;
    gate_threshold_at_execution: number;
  };
  execution: {
    agent_version_id: string;
    definition_sha256: string;
    provider: string;
    model: string;
    model_digest: string | null;
    temperature: number;
    seed: number | null;
    run_count: number;
    tool_definitions_sha256: string;
  };
  retrieval: {
    embedding_model: string;
    index_snapshot_id: string;
    corpus_manifest_sha256: string;
    top_k: number;
    embedding_provider?: string | null;
    embedding_dimensions?: number | null;
  } | null;
  comparison: { allowed_changes: string[]; required_invariants: string[] };
}

export type GateDiffChange = "new_failure" | "fixed" | "unchanged" | "new_case" | "removed";
export type GateComparisonStatus =
  | "comparable"
  | "conditions_changed"
  | "insufficient_evidence"
  | "no_baseline";

export interface GateLayerChange {
  layer: QualityLayer;
  change:
    | "new_failure"
    | "fixed"
    | "unchanged"
    | "new_case"
    | "removed"
    | "not_evaluated"
    | "not_comparable";
}

export interface GateDiffV1 {
  case_id: string;
  change: GateDiffChange;
  previous_passed: boolean | null;
  current_passed: boolean | null;
  answer_diff: string;
  previous_agent_version_id: string | null;
  current_agent_version_id: string;
  previous_run_ids: string[];
  current_run_ids: string[];
  previous_evidence_ids: string[];
  current_evidence_ids: string[];
  layer_changes: GateLayerChange[];
  comparison_status: GateComparisonStatus;
  changed_conditions: string[];
  previous_conditions_sha256: string | null;
  current_conditions_sha256: string | null;
}

export interface GateDetailV1 {
  case_id: string;
  result_case_id?: string | null;
  input: string;
  expected?: unknown;
  output: string | null;
  score: number;
  passed: boolean;
  error?: string | null;
  duration_ms?: number | null;
  tokens?: number | null;
  run_ids?: string[] | null;
  attempt_count?: number | null;
  failed_layers?: QualityLayer[] | null;
}

export interface GateRecordV1 {
  id: string;
  agent_version_id: string;
  suite_id: string;
  baseline_gate_id: string | null;
  status: string;
  quality_record_version: 1;
  conditions: GateConditions | null;
  details: GateDetailV1[];
  evidence: GateCaseEvidence[] | null;
  diff: GateDiffV1[];
}

export type FeedbackVerdict =
  | "needs_review"
  | "confirmed_fixed"
  | "not_fixed"
  | "conditions_changed";

export interface FeedbackResolution {
  id: string;
  feedback_id: string;
  gate_run_id: string;
  case_id: string;
  verdict: FeedbackVerdict;
  note: string;
  created_by: string;
  created_at: string;
}

export interface FeedbackRecord {
  id: string;
  run_id: string;
  agent_id: string;
  agent_version_id: string | null;
  rating: "positive" | "negative";
  expected_answer?: string | null;
  eval_case_id?: string | null;
  suite_id?: string | null;
  resolution_links?: FeedbackResolution[];
}

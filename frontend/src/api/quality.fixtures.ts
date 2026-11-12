/**
 * Compile-time compatibility fixtures for the pure quality contract slice.
 * The imported JSON is runtime-validated by backend/tests/test_quality_contracts.py;
 * these assertions are not a substitute for browser or API validation.
 */
import feedbackV1Json from "../../../docs/planning/quality_model/feedback-v1.json";
import gateV1Json from "../../../docs/planning/quality_model/gate-v1.json";
import legacyRunJson from "../../../docs/planning/quality_model/legacy-run.json";
import runV1Json from "../../../docs/planning/quality_model/run-v1.json";
import type { FeedbackItem, GateDiff, GateRun, Run, RunDetail } from "./client";
import type { FeedbackRecord, GateRecordV1, RunRecordV1 } from "./quality";

export const runV1Fixture = runV1Json as RunRecordV1;
export const gateV1Fixture = gateV1Json as GateRecordV1;
export const feedbackV1Fixture = feedbackV1Json as FeedbackRecord;
export const legacyRunFixture = legacyRunJson as RunDetail;

/** An old response remains valid when none of the additive fields is present. */
export const legacyRunWithoutQualityFields: RunDetail = {
  id: "33333333-3333-4333-8333-333333333330",
  agent_id: "11111111-1111-4111-8111-111111111111",
  agent_version_id: "22222222-2222-4222-8222-222222222222",
  input: "legacy question",
  output: "legacy answer",
  status: "success",
  step_count: 1,
  total_tokens: 12,
  duration_ms: 8,
  started_at: "2026-11-09T10:00:00Z",
  trace: [],
};

/** Structural typing models an old Reader ignoring unknown JSON keys. */
const legacyResponseWithUnknownQualityFields = {
  ...legacyRunWithoutQualityFields,
  answer_output: { answer: "not interpreted by the old Reader" },
};
export const oldRunProjection: Run = legacyResponseWithUnknownQualityFields;

export const legacyGateFixture: GateRun = {
  id: "55555555-5555-4555-8555-555555555555",
  agent_version_id: "22222222-2222-4222-8222-222222222223",
  suite_id: "44444444-4444-4444-8444-444444444444",
  baseline_gate_id: "55555555-5555-4555-8555-555555555554",
  trigger_source: "manual",
  status: "failed",
  pass_rate: 0,
  threshold: 0.7,
  failed_case_ids: ["kb-hit"],
  details: [
    {
      case_id: "kb-hit",
      input: "哪个版本？",
      output: "v1",
      score: 0,
      passed: false,
    },
  ],
  diff: [
    {
      case_id: "kb-hit",
      change: "new_failure",
      previous_passed: true,
      current_passed: false,
      answer_diff: "- v0\n+ v1",
    },
  ],
  quality_record_version: null,
  conditions: null,
  evidence: null,
  created_at: "2026-11-10T11:00:00Z",
};

export const legacyGateWithoutQualityFields: GateRun = {
  id: legacyGateFixture.id,
  agent_version_id: legacyGateFixture.agent_version_id,
  suite_id: legacyGateFixture.suite_id,
  baseline_gate_id: legacyGateFixture.baseline_gate_id,
  trigger_source: "manual",
  status: "failed",
  pass_rate: 0,
  threshold: 0.7,
  failed_case_ids: ["kb-hit"],
  details: legacyGateFixture.details,
  diff: legacyGateFixture.diff,
  created_at: legacyGateFixture.created_at,
};

export const legacyDiffFixture: GateDiff = {
  case_id: "kb-hit",
  change: "unchanged",
  answer_diff: "",
};

export const legacyFeedbackFixture: FeedbackItem = {
  id: "66666666-6666-4666-8666-666666666666",
  run_id: legacyRunFixture.id,
  agent_id: legacyRunFixture.agent_id,
  rating: "negative",
  context_snapshot: { input: "legacy question", output: "legacy answer" },
};

/** Future versions fit the additive envelope but cannot be typed as RunRecordV1. */
export const futureRunFixture: RunDetail = {
  ...legacyRunWithoutQualityFields,
  ...runV1Fixture,
  status: "success",
  trace: [],
  quality_record_version: 2,
};

export const futureGateFixture: GateRun & { future_assertion: { passed: boolean } } = {
  ...legacyGateWithoutQualityFields,
  quality_record_version: 2,
  future_assertion: { passed: true },
};

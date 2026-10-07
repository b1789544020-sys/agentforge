import { useTranslation } from "react-i18next";
import type { Step } from "../api/client";

interface Props {
  step: Step;
}

// Map backend `kind` (STEP_KIND_* in app/models.py) to a UI icon.
//  - think        → the LLM is reasoning / producing text
//  - tool_call    → the LLM asked to invoke a tool
//  - tool_result  → the tool returned
//  - final_answer → the LLM produced the final answer (no more tool calls)
//  - observation  → a recorded side-effect (e.g. memory write, error echo)
//  - error        → the run failed
const ICONS: Record<Step["kind"], string> = {
  think: "🧠",
  tool_call: "🛠",
  tool_result: "📥",
  final_answer: "✅",
  observation: "ℹ️",
  error: "❌",
};

// User-facing labels for the kind badge.  Keep in sync with `trace.*` i18n.
const KIND_LABEL: Record<Step["kind"], string> = {
  think: "think",
  tool_call: "tool",
  tool_result: "result",
  final_answer: "final",
  observation: "info",
  error: "error",
};

// 0.1.3 #9 — risk-class visual mapping for tool_call steps.
//
// The backend attaches the tool's risk class to every ``tool_call``
// step's payload (``payload["risk"]``) and surfaces it as the
// top-level ``step.risk`` field.  The trace UI only renders a badge
// for non-"safe" classes, so read-only / network / exec tools stand
// out in the panel without adding visual noise for harmless ones
// (echo / now / calculator / final_answer).
//
// ``safe`` is intentionally absent — it produces no badge.
const RISK_BADGE: Record<
  Exclude<NonNullable<Step["risk"]>, "safe" | null | undefined>,
  { className: string; label: string; symbol: string }
> = {
  read: { className: "badge warn-low", label: "read", symbol: "📖" },
  write: { className: "badge warn", label: "write", symbol: "✏️" },
  network: { className: "badge warn-high", label: "network", symbol: "🌐" },
  exec: { className: "badge danger", label: "exec", symbol: "⚡" },
};

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function StepNode({ step }: Props) {
  const { t } = useTranslation();
  const payload = step.payload ?? {};
  const toolArgs =
    step.kind === "tool_call" ? (payload.arguments as Record<string, unknown> | undefined) : undefined;
  const toolResult = step.kind === "tool_result" ? payload.result : undefined;
  const showArgs = toolArgs !== undefined;
  const showResult = toolResult !== undefined;
  // 0.1.3 #9 — only show a coloured risk badge for non-"safe" tool
  // calls; ``safe`` (and the undefined/null legacy case) gets no
  // badge so the trace panel doesn't drown in green for the common
  // case of harmless tools.
  const riskKey: keyof typeof RISK_BADGE | null =
    step.kind === "tool_call" &&
    step.risk &&
    step.risk !== "safe"
      ? (step.risk as keyof typeof RISK_BADGE)
      : null;
  const riskBadge = riskKey ? RISK_BADGE[riskKey] : null;

  return (
    <div className="step">
      <div className="step-head">
        <div className="step-title">
          <span className="step-icon" aria-hidden="true">{ICONS[step.kind]}</span>
          <strong>#{step.index}</strong>
          <span className="badge accent">{KIND_LABEL[step.kind]}</span>
          {step.name && <span className="badge">{step.name}</span>}
          {riskBadge && (
            <span
              className={riskBadge.className}
              title={`Risk class: ${riskBadge.label}`}
              aria-label={`Risk: ${riskBadge.label}`}
            >
              <span aria-hidden="true">{riskBadge.symbol}</span> {riskBadge.label}
            </span>
          )}
        </div>
        <div className="muted">
          {step.duration_ms != null && step.duration_ms > 0 && (
            <span>{step.duration_ms} ms</span>
          )}
          {step.tokens != null && step.tokens > 0 && (
            <span> · {step.tokens} tok</span>
          )}
        </div>
      </div>

      {step.content && (
        <div className="step-body">{step.content}</div>
      )}
      {showArgs && (
        <details>
          <summary className="muted">{t("trace.args")}</summary>
          <pre>{safeStringify(toolArgs)}</pre>
        </details>
      )}
      {showResult && (
        <details>
          <summary className="muted">{t("trace.result")}</summary>
          <pre>{safeStringify(toolResult)}</pre>
        </details>
      )}
    </div>
  );
}
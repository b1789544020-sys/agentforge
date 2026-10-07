import StepNode from "./StepNode";
import type { Step } from "../api/client";
import { useTranslation } from "react-i18next";

interface Props {
  steps: Step[];
}

export default function TraceTree({ steps }: Props) {
  const { t } = useTranslation();
  if (steps.length === 0) {
    return (
      <div className="empty">
        <div className="empty-icon" aria-hidden="true">🪜</div>
        <div className="empty-title">{t("trace.no_steps")}</div>
      </div>
    );
  }
  return (
    <div>
      {steps.map((s) => (
        <StepNode key={s.id} step={s} />
      ))}
    </div>
  );
}
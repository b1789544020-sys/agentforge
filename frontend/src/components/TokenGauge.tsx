import { useTranslation } from "react-i18next";

interface Props {
  used: number;
  budget: number;
  label?: string;
}

export default function TokenGauge({ used, budget, label }: Props) {
  const { t } = useTranslation();
  const pct = budget <= 0 ? 0 : Math.min(100, Math.round((used / budget) * 100));
  const color = pct > 90 ? "var(--err)" : pct > 70 ? "var(--warn)" : "var(--ok)";
  const display = label ?? t("tokens.label");

  return (
    <div className="gauge">
      <div className="row between">
        <strong>{display}</strong>
        <span className="muted">
          {used.toLocaleString()} / {budget.toLocaleString()} ({pct}%)
        </span>
      </div>
      <div className="gauge-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div
          className="gauge-fill"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
    </div>
  );
}
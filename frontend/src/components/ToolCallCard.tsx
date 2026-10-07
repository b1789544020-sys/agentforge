import { useTranslation } from "react-i18next";

interface Props {
  toolName: string;
  args?: Record<string, unknown>;
  result?: unknown;
  error?: string;
  durationMs?: number;
}

export default function ToolCallCard({ toolName, args, result, error, durationMs }: Props) {
  const { t } = useTranslation();
  return (
    <div className={`tool-card${error ? " error" : ""}`}>
      <div className="tool-card-head">
        <div className="row" style={{ gap: 8 }}>
          <span aria-hidden="true">🛠</span>
          <strong>{toolName}</strong>
        </div>
        {durationMs != null && <span className="muted">{durationMs} ms</span>}
      </div>
      {args != null && (
        <details>
          <summary className="muted">{t("trace.args")}</summary>
          <pre>{JSON.stringify(args, null, 2)}</pre>
        </details>
      )}
      {error ? (
        <pre style={{ color: "#fca5a5" }}>{error}</pre>
      ) : (
        result != null && (
          <details open>
            <summary className="muted">{t("trace.result")}</summary>
            <pre>{JSON.stringify(result, null, 2)}</pre>
          </details>
        )
      )}
    </div>
  );
}
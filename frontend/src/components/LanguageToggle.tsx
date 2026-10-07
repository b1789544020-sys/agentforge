import { useTranslation } from "react-i18next";
import { getCurrentLanguage, setLanguage } from "../i18n";

export default function LanguageToggle() {
  const { t, i18n } = useTranslation();
  const current = (i18n.language as "en" | "zh") ?? getCurrentLanguage();
  const next: "en" | "zh" = current === "en" ? "zh" : "en";
  const label = t("app.lang_toggle");

  return (
    <button
      type="button"
      className="lang-toggle"
      onClick={() => setLanguage(next)}
      title={current === "en" ? "切换到中文" : "Switch to English"}
    >
      🌐 {label}
    </button>
  );
}

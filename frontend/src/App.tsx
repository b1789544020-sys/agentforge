import { useState } from "react";
import { useTranslation } from "react-i18next";
import AgentConfig from "./pages/AgentConfig";
import AgentRun from "./pages/AgentRun";
import Evaluation from "./pages/Evaluation";
import LanguageToggle from "./components/LanguageToggle";

type Tab = "config" | "run" | "eval";

export default function App() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("config");

  const TABS: { id: Tab; label: string }[] = [
    { id: "config", label: t("app.tab_config") },
    { id: "run", label: t("app.tab_run") },
    { id: "eval", label: t("app.tab_eval") },
  ];

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-brand">
          <h1>🔥 {t("app.title")}</h1>
          <span className="topbar-tagline">· {t("app.tagline")}</span>
        </div>
        <LanguageToggle />
      </header>

      <nav className="tabs" role="tablist">
        {TABS.map((tt) => (
          <button
            key={tt.id}
            role="tab"
            aria-selected={tab === tt.id}
            className={`tab ${tab === tt.id ? "active" : ""}`}
            onClick={() => setTab(tt.id)}
          >
            {tt.label}
          </button>
        ))}
      </nav>

      <main>
        {tab === "config" && <AgentConfig />}
        {tab === "run" && <AgentRun />}
        {tab === "eval" && <Evaluation />}
      </main>
    </div>
  );
}
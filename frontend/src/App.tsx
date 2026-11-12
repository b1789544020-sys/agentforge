import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import AgentConfig from "./pages/AgentConfig";
import AgentRun, { type RunFocus } from "./pages/AgentRun";
import AgentStudio from "./pages/AgentStudio";
import Evaluation from "./pages/Evaluation";
import PromptTemplates from "./pages/PromptTemplates";
import QualityGatePanel from "./pages/QualityGates";
import LanguageToggle from "./components/LanguageToggle";
import TeamManagement from "./pages/TeamManagement";
import OperationsPage from "./pages/Operations";
import DeliveryCenter from "./pages/DeliveryCenter";
import KnowledgePage from "./pages/Knowledge";
import { Auth, type User } from "./api/client";

type Tab = "config" | "studio" | "run" | "eval" | "prompts" | "gates" | "teams" | "operations" | "delivery" | "knowledge";

export default function App() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("config");
  const [runFocus, setRunFocus] = useState<RunFocus | null>(null);
  const [user, setUser] = useState<User | null>(null);
  useEffect(() => { Auth.me().then(setUser).catch(() => setUser(null)); }, []);
  const can = (permission: string) => !user || !!user.permissions?.includes("*") || !!user.permissions?.includes(permission);

  const TABS: { id: Tab; label: string }[] = [
    { id: "config", label: t("app.tab_config") },
    { id: "studio", label: t("app.tab_studio") },
    { id: "run", label: t("app.tab_run") },
    { id: "eval", label: t("app.tab_eval") },
    { id: "prompts", label: t("app.tab_prompts") },
    { id: "gates", label: "质量门禁" },
    { id: "teams", label: "团队与审计" },
    { id: "operations", label: "运营与成本" },
    { id: "delivery", label: "交付与模板" },
    { id: "knowledge", label: "知识库" },
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
        {tab === "config" && <AgentConfig canCreate={can("agent:create")} canDelete={can("agent:delete")} />}
        {tab === "studio" && <AgentStudio />}
        {tab === "run" && <AgentRun focus={runFocus} />}
        {tab === "eval" && <Evaluation />}
        {tab === "prompts" && <PromptTemplates />}
        {tab === "gates" && <QualityGatePanel onOpenRun={(agentId, runId, versionId) => {
          setRunFocus({ agentId, runId, versionId, key: Date.now() });
          setTab("run");
        }} />}
        {tab === "teams" && <TeamManagement permissions={user?.permissions || []} />}
        {tab === "operations" && <OperationsPage permissions={user?.permissions || []} />}
        {tab === "delivery" && <DeliveryCenter />}
        {tab === "knowledge" && <KnowledgePage />}
      </main>
    </div>
  );
}
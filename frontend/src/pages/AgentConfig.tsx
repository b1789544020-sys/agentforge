import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useAgentStore } from "../stores/agentStore";
import { Teams, type Agent, type Team } from "../api/client";

export default function AgentConfig({ canCreate = true, canDelete = true }: { canCreate?: boolean; canDelete?: boolean }) {
  const { t } = useTranslation();
  const { agents, tools, loading, error, loadAgents, loadTools, createAgent, deleteAgent } =
    useAgentStore();
  const [name, setName] = useState("");
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamId, setTeamId] = useState("");
  const [description, setDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("You are a helpful assistant.");
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [model, setModel] = useState("gpt-4o-mini");
  const [provider, setProvider] = useState("openai");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);

  useEffect(() => {
    loadAgents();
    loadTools();
    Teams.list().then((rows) => {
      setTeams(rows);
      if (rows.length) setTeamId((current) => current || rows[0].id);
    }).catch(() => setTeams([]));
  }, [loadAgents, loadTools]);

  const toggleTool = (toolName: string) => {
    setSelectedTools((prev) =>
      prev.includes(toolName) ? prev.filter((t) => t !== toolName) : [...prev, toolName],
    );
  };

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setNameError(t("config.name_required", { defaultValue: "Please enter a name." }));
      return;
    }
    setNameError(null);
    setSaving(true);
    try {
      await createAgent({
        name: name.trim(),
        team_id: teamId || undefined,
        description: description.trim() || undefined,
        system_prompt: systemPrompt,
        tools: selectedTools,
        model,
        provider,
        config: {
          ...((["ollama", "vllm", "local", "openai-compatible"].includes(provider) && baseUrl.trim())
            ? { base_url: baseUrl.trim() } : {}),
          ...(apiKey ? { api_key: apiKey } : {}),
        },
      });
      setName("");
      setDescription("");
      setApiKey("");
    } catch {
      // store already exposes `error`; keep the form open so the user can retry.
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page">
      <h2>{t("config.title")}</h2>
      {error && <div className="banner err" role="alert">{error}</div>}

      {canCreate && <section className="card">
        <h3>{t("config.new_agent")}</h3>
        <form onSubmit={onCreate} className="form" noValidate>
          {teams.length > 0 && <label>所属团队
            <select value={teamId} onChange={(event) => setTeamId(event.target.value)}>
              {teams.map((team) => <option key={team.id} value={team.id}>{team.name}</option>)}
            </select>
          </label>}
          <label>
            {t("config.name")}
            <input
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (nameError) setNameError(null);
              }}
              placeholder={t("config.name_placeholder", { defaultValue: "e.g. Research Assistant" })}
              aria-invalid={nameError ? "true" : "false"}
            />
            {nameError && <span className="banner err" style={{ padding: "6px 10px", fontSize: 12 }}>{nameError}</span>}
          </label>
          <label>
            {t("config.description")}
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("config.description_placeholder", { defaultValue: "Short description" })}
            />
          </label>
          <label>Provider
            <select value={provider} onChange={(event) => setProvider(event.target.value)}>
              {(["openai", "mock", "ollama", "vllm", "openai-compatible", "deepseek", "qwen", "kimi", "glm", "doubao", "anthropic"]).map((item) =>
                <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          <label>
            {t("config.model")}
            <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="gpt-4o-mini / qwen2.5:3b" />
          </label>
          {["ollama", "vllm", "local", "openai-compatible"].includes(provider) && <label>本地 / 兼容 endpoint（/v1）
            <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="http://localhost:11434/v1" />
          </label>}
          {provider !== "mock" && <label>API key（可选，落库加密）
            <input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} />
          </label>}
          <label>
            {t("config.system_prompt")}
            <textarea
              rows={4}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
            />
          </label>
          <fieldset>
            <legend>{t("config.tools")}</legend>
            <div className="row wrap">
              {tools.length === 0 ? (
                <span className="empty" style={{ width: "100%", padding: "16px 12px" }}>
                  <span className="empty-title">{t("config.no_tools")}</span>
                </span>
              ) : (
                tools.map((tt) => {
                  const checked = selectedTools.includes(tt.name);
                  return (
                    <label key={tt.name} className={`chip ${checked ? "checked" : ""}`} title={tt.description}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleTool(tt.name)}
                      />
                      {tt.name}
                    </label>
                  );
                })
              )}
            </div>
          </fieldset>
          <div className="form-actions">
            <button type="submit" disabled={saving} className="lg">
              {saving ? t("config.saving") : t("config.create")}
            </button>
          </div>
        </form>
      </section>}

      <section className="card">
        <h3>
          {t("config.existing")}
          {loading && <span className="muted">{t("config.loading")}</span>}
        </h3>
        {agents.length === 0 ? (
          <div className="empty">
            <div className="empty-icon" aria-hidden="true">🪪</div>
            <div className="empty-title">{t("config.empty")}</div>
            <div className="empty-sub">{t("config.empty_sub", { defaultValue: "Create your first agent using the form above." })}</div>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>{t("config.col_name")}</th>
                <th>{t("config.col_model")}</th>
                <th>{t("config.col_tools")}</th>
                <th>{t("config.col_created")}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {agents.map((a: Agent) => (
                <tr key={a.id}>
                  <td>
                    <strong>{a.name}</strong>
                    {a.description && <span className="muted">{a.description}</span>}
                  </td>
                  <td>
                    <code>{a.model}</code>
                  </td>
                  <td>{a.tools?.length ?? 0}</td>
                  <td className="muted">{new Date(a.created_at).toLocaleString()}</td>
                  <td style={{ textAlign: "right" }}>
                    {canDelete && <button onClick={() => deleteAgent(a.id)} className="danger icon-only" title={t("config.delete")}>
                      🗑
                    </button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
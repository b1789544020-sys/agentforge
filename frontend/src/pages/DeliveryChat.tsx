import axios from "axios";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";

type Message = { role: "user" | "assistant"; content: string; traceId?: string };

function deliveryError(reason: unknown): string {
  if (axios.isAxiosError(reason)) {
    const status = reason.response?.status;
    const retryAfter = Number(reason.response?.headers["retry-after"]);
    if (status === 429) {
      return Number.isFinite(retryAfter) && retryAfter > 0
        ? `请求过于频繁，请约 ${Math.ceil(retryAfter)} 秒后重试。`
        : "请求过于频繁，请稍后重试。";
    }
    if (status === 503) return "交付服务暂时不可用，请稍后重试。";
    if (status === 410) return "此交付链接已吊销，请联系发布方获取新链接。";
    if (status === 403) return "此交付应用已停用，请联系发布方。";
    if (status === 404) return "交付链接不存在或已失效。";
  }
  return reason instanceof Error ? reason.message : String(reason);
}

export default function DeliveryChat({ embedded = false }: { embedded?: boolean }) {
  const { token = "" } = useParams();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    const question = input.trim();
    if (!question || busy || !token) return;
    setMessages((rows) => [...rows, { role: "user", content: question }]);
    setInput(""); setBusy(true); setError("");
    try {
      const response = await api.post<{ output: string; trace_id: string }>(`/deliver/${token}/chat`, { input: question });
      setMessages((rows) => [...rows, { role: "assistant", content: response.data.output, traceId: response.data.trace_id }]);
    } catch (reason) {
      setError(deliveryError(reason));
    } finally { setBusy(false); }
  };

  return (
    <div className={`delivery-chat ${embedded ? "embedded" : ""}`}>
      <header className="delivery-header"><strong>🤖 AI 助手</strong><span>由 AgentForge 提供 · 不是真人客服</span></header>
      <main className="delivery-messages" aria-live="polite">
        {!messages.length && <div className="empty">请输入问题开始对话</div>}
        {messages.map((message, index) => <div key={index} className={`chat-message ${message.role}`}>
          <strong>{message.role === "user" ? "你" : "AI 助手"}</strong><p>{message.content}</p>
          {message.traceId && <small>trace: {message.traceId}</small>}
        </div>)}
        {busy && <p className="muted">AI 正在回答…</p>}
      </main>
      {error && <div className="banner err" role="alert">{error}</div>}
      <form onSubmit={send} className="delivery-input">
        <input value={input} onChange={(event) => setInput(event.target.value)} placeholder="输入消息…" aria-label="输入消息" maxLength={32_000} />
        <button type="submit" disabled={busy || !input.trim()}>发送</button>
      </form>
    </div>
  );
}

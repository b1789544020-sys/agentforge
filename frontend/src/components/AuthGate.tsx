import { useEffect, useState, type ReactNode } from "react";
import { Auth, type User } from "../api/client";

export default function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [register, setRegister] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Auth.me().then(setUser).catch(() => setNeedsLogin(true)).finally(() => setChecking(false));
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const authenticated = register
        ? await Auth.register(email, password)
        : await Auth.login(email, password);
      setUser(authenticated);
      setNeedsLogin(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (checking) return <div className="page"><div className="empty">正在检查登录状态…</div></div>;
  if (needsLogin && !user) {
    return <div className="page" style={{ maxWidth: 480, margin: "60px auto" }}>
      <section className="card">
        <h2>{register ? "注册 AgentForge" : "登录 AgentForge"}</h2>
        {error && <div className="banner err" role="alert">{error}</div>}
        <form onSubmit={submit} className="form">
          <label>邮箱<input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
          <label>密码（至少 8 位）<input type="password" required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} /></label>
          <button type="submit" disabled={busy}>{busy ? "请稍候…" : register ? "注册并登录" : "登录"}</button>
        </form>
        <button className="secondary" onClick={() => setRegister((value) => !value)}>
          {register ? "已有账号？去登录" : "没有账号？去注册"}
        </button>
      </section>
    </div>;
  }
  return <>
    {children}
    <button className="secondary" style={{ position: "fixed", right: 16, bottom: 16 }} onClick={() => {
      Auth.logout();
      window.location.reload();
    }}>退出{user?.email ? `（${user.email}）` : ""}</button>
  </>;
}

import { useEffect, useState } from "react";
import { Teams, type AuditEntry, type Team, type TeamMember } from "../api/client";

export default function TeamManagement({ permissions = [] }: { permissions?: string[] }) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamId, setTeamId] = useState("");
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [audits, setAudits] = useState<AuditEntry[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("developer");
  const [action, setAction] = useState("");
  const [orgName, setOrgName] = useState("");
  const [teamName, setTeamName] = useState("");
  const [error, setError] = useState("");
  const globalAdmin = permissions.includes("*");
  const canManage = globalAdmin || permissions.includes("member:manage");
  const canAudit = globalAdmin || permissions.includes("audit:read");

  useEffect(() => {
    Teams.list().then((rows) => {
      setTeams(rows); setTeamId((current) => current || rows[0]?.id || "");
    }).catch((reason) => setError(String(reason)));
  }, []);
  useEffect(() => {
    if (!teamId) { setMembers([]); setAudits([]); return; }
    Teams.members(teamId).then(setMembers).catch((reason) => setError(String(reason)));
    if (canAudit) Teams.audit(teamId, action).then(setAudits).catch((reason) => setError(String(reason)));
  }, [teamId, action, canAudit]);

  const add = async () => {
    if (!teamId || !email.trim()) return;
    try {
      await Teams.addMember(teamId, email.trim(), role);
      setEmail(""); setMembers(await Teams.members(teamId));
    } catch (reason) { setError(String(reason)); }
  };
  const changeRole = async (userId: string, nextRole: string) => {
    try {
      await Teams.setRole(teamId, userId, nextRole);
      setMembers(await Teams.members(teamId));
    } catch (reason) { setError(String(reason)); }
  };
  const remove = async (userId: string) => {
    try {
      await Teams.removeMember(teamId, userId);
      setMembers(await Teams.members(teamId));
    } catch (reason) { setError(String(reason)); }
  };
  const createTeam = async () => {
    if (!orgName.trim() || !teamName.trim()) return;
    try {
      const org = await Teams.createOrg(orgName.trim());
      const team = await Teams.create(org.id, teamName.trim());
      setTeams((rows) => [...rows, team]); setTeamId(team.id);
      setOrgName(""); setTeamName("");
    } catch (reason) { setError(String(reason)); }
  };

  return (
    <div className="page">
      <h2>团队与审计</h2>
      {error && <div className="banner err" role="alert">{error}</div>}
      {globalAdmin && <section className="card"><h3>创建组织与团队</h3>
        <div className="row wrap">
          <input placeholder="组织名称" value={orgName} onChange={(event) => setOrgName(event.target.value)} />
          <input placeholder="团队名称" value={teamName} onChange={(event) => setTeamName(event.target.value)} />
          <button onClick={createTeam}>创建</button>
        </div>
      </section>}
      <section className="card">
        <label>当前团队
          <select value={teamId} onChange={(event) => setTeamId(event.target.value)}>
            {!teams.length && <option value="">暂无团队</option>}
            {teams.map((team) => <option value={team.id} key={team.id}>{team.name} · {team.role}</option>)}
          </select>
        </label>
      </section>
      <section className="card"><h3>成员</h3>
        {canManage && <div className="row wrap">
          <input type="email" placeholder="已注册用户邮箱" value={email} onChange={(event) => setEmail(event.target.value)} />
          <select value={role} onChange={(event) => setRole(event.target.value)}>
            <option value="developer">developer</option><option value="viewer">viewer</option><option value="team-admin">team-admin</option>
          </select>
          <button onClick={add} disabled={!teamId}>邀请成员</button>
        </div>}
        <table><thead><tr><th>邮箱</th><th>角色</th>{canManage && <th />}</tr></thead>
          <tbody>{members.map((member) => <tr key={member.id}>
            <td>{member.email || member.user_id}</td>
            <td>{canManage ? <select value={member.role} onChange={(event) => changeRole(member.user_id, event.target.value)}>
              <option value="developer">developer</option><option value="viewer">viewer</option><option value="team-admin">team-admin</option>
            </select> : member.role}</td>
            {canManage && <td><button className="danger" onClick={() => remove(member.user_id)}>移除</button></td>}
          </tr>)}</tbody></table>
      </section>
      {canAudit && <section className="card"><h3>审计日志</h3>
        <input placeholder="按 action 筛选，如 agent.delete" value={action} onChange={(event) => setAction(event.target.value)} />
        <table><thead><tr><th>时间</th><th>操作</th><th>Actor</th><th>目标</th><th>详情</th></tr></thead>
          <tbody>{audits.map((row) => <tr key={row.id}>
            <td>{new Date(row.created_at).toLocaleString()}</td><td><code>{row.action}</code></td>
            <td>{row.actor_id || "system"}</td><td>{row.target_type}/{row.target_id}</td>
            <td><code>{JSON.stringify(row.detail)}</code></td>
          </tr>)}</tbody></table>
      </section>}
    </div>
  );
}

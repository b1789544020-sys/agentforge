# 多团队 RBAC、审计与高风险授权

## 层级与角色

层级为 `Organization → Team → Membership`。成员必须对应已注册用户；一个用户可属于多个团队，在同一团队内有一个角色。全局 `admin` 等价于 `super-admin`，团队角色如下：

| 权限 | team-admin | developer | viewer |
| --- | --- | --- | --- |
| Agent 读取 | 是 | 是 | 是 |
| Agent 创建/修改 | 是 | 是 | 否 |
| Agent 删除 | 团队内任意 | 仅自己创建 | 否 |
| Run 执行/暂停/取消 | 是 | 是 | 否 |
| Eval 创建/运行、Memory 写入 | 是 | 是 | 否 |
| Gate 读取/触发 | 是 | 是 | 仅读取 |
| Gate 绕过 | 是，且写审计 | 否 | 否 |
| 成员管理、审计读取、授权批准 | 是 | 否 | 否 |

唯一权限矩阵位于 `backend/app/rbac.py::ROLE_PERMISSIONS`。HTTP 方法到权限点的映射、团队成员查询和 Agent scope 也在该模块，不在页面中做安全判断。前端只根据 `/auth/me` 返回的权限点隐藏入口，后端仍会再次拒绝越权请求。

## 强制数据边界

- Agent、AgentVersion、Run、EvalRun、EvalCase、Memory 和 ToolMetadata 均带 `team_id`；Agent 的创建、列表、导出和导入统一使用团队 scope。
- 嵌套 Run/Eval/Memory/Gate/Prompt 路由先校验父 Agent 的团队与操作权限，不能靠猜测子资源 ID 绕过。
- 列表只返回当前用户团队及本人旧个人空间的数据。跨团队详情返回 404，同团队但角色不足返回 403。
- Suite 只能绑定同团队 EvalCase；Suite 列表、详情、编辑和复制同样执行团队隔离。
- WebSocket 在 accept 前检查 Membership，不能通过 run ID 订阅其他团队 trace。
- 为兼容旧安装，没有 Membership 的用户保留 owner 隔离的个人空间；加入团队后，团队资源由 Membership 决定，旧个人资源仍仅本人可见。

## 团队与审计 API

- `POST/GET /orgs`：创建/查询组织（创建仅 super-admin）。
- `POST/GET /teams`：创建/查询团队（创建仅 super-admin）。
- `GET/POST /teams/{team_id}/members`：成员列表、邀请已注册用户。
- `PATCH/DELETE /teams/{team_id}/members/{user_id}`：调整角色、移除成员；最后一个 team-admin 不可移除。
- `GET /audit`：按 team、actor、action、since、until 过滤。只有 super-admin 或本团队 team-admin 可读。
- `AuditLog` 没有公开创建、修改或删除接口，ORM 也拒绝 update/delete。Agent、成员、门禁、授权和危险工具操作由服务端内部写入。

## 高风险授权

`ApprovalRequest` 支持 `tool.enable`、`gate.override`、`resource.transfer`、`deployment.publish` 动作类型。最小完整执行链路已用于危险工具：

1. developer 调用 `POST /tools/{name}/enable-request`，提供 `team_id` 和理由；响应 202，工具保持原状态。
2. team-admin/super-admin 调用 `POST /approvals/{id}/approve`；也可 `reject`。
3. 批准后工具才设为 enabled，同时写入 `tool.enable` 和 `approval.approved` 审计。

`POST /approvals` 也可创建其他高风险请求记录；跨团队转移和生产发布的实际执行由对应资源工作流消费，不能把“请求已批准”误当作这些资源已经变更。

## 验证与未验证项

```bash
python -m pytest backend/tests/test_direction2.py -q
```

测试使用真实 JWT 和两个团队，覆盖角色矩阵、猜测 ID、列表隔离、Run/Eval/Memory 边界、Suite 用例绑定、删除权限、审计不可变和危险工具审批；既有 Stage 4 回归另行覆盖 WebSocket owner 隔离。PostgreSQL 并发成员变更、生产反向代理下的团队 WebSocket 以及多实例 worker 的团队隔离仍需目标环境复核。

# 质量门禁与版本回归

## 核心对象

- `AgentVersion`：Agent 每次创建、更新或导入保存时追加的不可变运行快照，包含提示词、Provider/模型、温度、最大步数、工具和完整配置。只有 `gate_status` 可以随后变更。
- `EvalSuite`：固定 EvalCase ID 集合、结构化 scorer、通过率阈值、启停和触发策略。可复制为默认停用的新套件。
- `GateRun`：一个版本在一个套件上的持久判定，包含通过率、失败用例、逐条输出和相对上一版本的 diff。
- `GatePolicy` / `GateBypass`：全局默认阈值、强制开关、通知接收人和允许绕过配置；管理员绕过必须填写理由并追加独立记录。

## API

所有路由同时挂载在 `/api` 和 `/api/v1` 下。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /agents/{agent_id}/versions` | 查询不可变版本及 `gate_status` |
| `POST /suites` | 创建套件并绑定固定用例 |
| `GET/PATCH/DELETE /suites/{id}` | 查询、编辑、启停或删除套件 |
| `POST /suites/{id}/copy` | 复制为默认停用的套件 |
| `POST /agents/{agent_id}/gates` | 以 `agent_version_id + suite_id` 手动触发 |
| `GET /agents/{agent_id}/gates` | 查询历史，可按 `suite_id` 过滤 |
| `GET /agents/{agent_id}/gates/{gate_id}` | 查询失败明细和版本 diff |
| `POST /agents/{agent_id}/gates/{gate_id}/reevaluate` | 按套件当前阈值重判已有通过率，不重跑模型 |
| `GET/PATCH /gate-policy` | 查看或调整全局策略（修改仅管理员） |
| `POST /agents/{agent_id}/versions/{version_id}/bypass` | 管理员带理由绕过（追加审计记录） |
| `POST /gates/run-due` | 管理员手动扫描到期定时门禁 |

套件的 `auto_on_save=true` 时，保存版本自动创建 `trigger_source=save` 的门禁。设置 `schedule_interval_seconds` 和套件所属 Agent 后，Celery Beat 每分钟扫描定时任务；单进程环境也可调用到期扫描接口。手动、自动、定时三种触发共用 `app.gates.service`，不会采用不同判定逻辑。

## 判定与 diff

每条用例先按 `0.7` 的结构化评分门槛得到 pass/fail；套件通过率为 `passed / total`，达到套件阈值才为 `passed`。当前版本与同一 Agent、同一套件的上一个已完成版本比较，分类为 `new_failure`、`fixed`、`unchanged`、`new_case` 或 `removed`，同时保存 unified answer diff。多个套件的最新执行未全部完成时版本为 `pending`，任一失败则版本为 `failed`，全部通过才为 `passed`。

调整阈值不会暗中改写原始用例结果；必须显式调用 `reevaluate`，响应会记录新阈值及重判状态。绕过只把版本状态改为 `bypassed`，不会把失败的 GateRun 伪装成通过。

## 验证

```bash
python -m pytest backend/tests/test_direction1.py -q
python scripts/tests/quality_gate_e2e.py
```

端到端脚本通过离线 `mock_replies` 真实经过 AgentLoop 和 EvalRunner：首版四用例全过，第二版两条失败，门禁结果为 50% 且 diff 精确标出两条 `new_failure`。这不是实际 Provider 效果验收。真实 Celery/Redis 调度、真实模型回归及生产浏览器操作仍需在目标环境验证。

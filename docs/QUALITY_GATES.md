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

## 四层质量记录 v1（已实施）

[16.10 四层质量数据模型 v1](planning/QUALITY_DATA_MODEL.md)的存储、执行采集、API 和 UI 已落地，migration head 为 `b72f41a9c012`（0012）。

- **Run 原始观察**：`quality_record_version` 及可空 `answer_output/tool_output/retrieval_output/execution_output`。回答引用只关联实际注入的来源切片；工具记录来自持久化 Step；系统记录来自真实 Run 终态。重复 Provider call ID 会按调用顺序生成唯一映射，原始 Step 不丢失。
- **Gate 判定**：创建时冻结用例快照、scorer、模型、解码和检索条件；终态保存逐 case `evidence`。默认必需 output/system，声明工具或知识检索时加入对应必需层。总分不能覆盖系统、工具或必需检索失败。
- **层级 diff**：保留旧 `change/answer_diff`，新增两侧版本、Run/evidence ID、四层变化、条件哈希和 `comparison_status`。模型、套件、检索等未声明条件变化时标为 `conditions_changed`；legacy 证据不足时标为 `insufficient_evidence`，不伪装成 `fixed/unchanged`。
- **旧记录兼容**：0011 行的质量列保持 `NULL`，Run/Gate 列表仍返回旧摘要且不加载大字段，详情才返回质量对象。`NULL` 表示 legacy/未采集，不表示通过。当前每案执行一次；契约中的多 attempt 结构不等于已实现多轮波动统计。
- **反馈人工复核**：Feedback 固定 `Run.agent_version_id`。门禁 Run 的反馈转换会复用原 EvalCase；外部 Run 才新建用例。`GET/POST /operations/feedback/{id}/resolutions` 提供追加式人工记录，`confirmed_fixed` 要求原负面 Run 位于 comparable diff 的基线侧、当前全部 attempt 通过、对应用例和人工理由。自动 Gate 不会生成该结论。

Run 观察与 Gate 判定在 UI 中分开展示。检索层当前断言“授权检索已完成”并保存候选/注入证据，不声称已经解决语义相关性、引用支持度或真实研究质量校准。

## 验证

```bash
python -m pytest backend/tests/test_direction1.py -q
python -m pytest backend/tests/test_quality_persistence.py backend/tests/test_feedback_resolution.py -q
python scripts/tests/quality_gate_e2e.py
python scripts/tests/quality_browser_e2e.py
```

端到端脚本通过离线 `mock_replies` 真实经过 AgentLoop 和 EvalRunner：首版四用例全过，第二版两条失败，门禁结果为 50% 且 diff 精确标出两条 `new_failure`。这不是实际 Provider 效果验收。真实 Celery/Redis 调度、真实模型回归及生产浏览器操作仍需在目标环境验证。

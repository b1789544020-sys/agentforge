# 运营指标、成本、坏样本与告警

## 指标口径

`GET /api/operations/metrics` 直接复用持久化 Run，不复制第二份埋点数据。支持 `agent_id`、`team_id`、`model`、`since/until` 和 `group=hour/day/week`；非 super-admin 的 Run 查询会强制注入团队/个人 scope。

- 请求量：筛选范围内全部 Run；成功状态为 `success/completed/ok`，失败状态为 `failed/error/terminated`。
- 拒绝数：错误文本包含权限拒绝、blocked/rejected 或 sandbox 拦截的失败 Run；拒绝数同时可能计入失败数。
- Token：分别汇总 prompt、completion 和 total；耗时给出平均与 p95。
- 返回 `summary`、按时间 bucket 的 `trends`，以及按 Agent/Team/模型的 `dimensions`。大数据量生产环境应增加时间分区/预聚合，当前实现优先保证口径一致。

## 历史成本

`POST /api/operations/prices` 仅 super-admin 可写，价格按 `provider + model + effective_from` 追加，单位是**每百万输入/输出 token**：

```text
cost = (prompt_tokens × input_per_million + completion_tokens × output_per_million) / 1,000,000
```

每次 Run 选择开始时间之前最新生效的价格，不会用今天的价格错误重算昨天。`GET /api/operations/cost` 返回总额、日趋势、TOP Agent、模型分布及 `unpriced_tokens`。没有匹配价格的 token 不会按零成本隐藏，而是单独报告；多币种返回 `MIXED`，不应直接相加做财务结算。

## 坏样本闭环

1. `POST /api/operations/feedback` 以 `run_id + rating` 标记好/坏。服务端保存当时输入、输出和 AgentVersion 快照；读取时敏感配置仍脱敏。
2. `GET /api/operations/feedback?rating=negative` 查看坏样本。
3. `POST /api/operations/feedback/{id}/convert` 提交人工修正答案，生成带 `source_feedback_id` 的 EvalCase；可选 `suite_id` 将同一 case ID 追加到回归套件。
4. 下一次门禁自然读取更新后的 Suite case_ids，不需要手工复制问题或版本。

反馈和转换都写审计，且只能在当前团队 Run/Suite 范围内执行。

## 告警与站内通知

规则类型：`failure_rate`（24h 失败率）、`daily_cost`（UTC 当日成本）、`error_spike`（当前小时相对前 24h 的比例）、`gate_failures`（连续 N 次门禁失败）。

- `GET/POST /api/operations/alerts/rules` 管理规则；非全局管理员需要本团队 `approval:manage`。
- Celery Beat 每分钟评估；super-admin 也可 `POST /api/operations/alerts/evaluate` 主动评估。
- 触发后写入 `AlertEvent` 即站内通知。相同规则已有 open 事件时不重复创建；`POST /api/operations/alerts/events/{id}/ack` 确认后可再次告警。
- `GET /api/operations/alerts/events` 只返回当前团队事件。未实现 webhook，不得将站内事件描述为已发送外部通知。

## 验证与未验证项

```bash
python -m pytest backend/tests/test_direction4.py -q
```

测试核对失败率、拒绝数、token、两期单价精确成本、差评→修正→EvalCase→Suite，以及告警去重和确认。真实大流量预聚合性能、财务对账、外部告警通道和生产浏览器图表交互尚未验收。

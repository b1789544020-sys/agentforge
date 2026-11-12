# 四层质量数据模型 v1（16.10 设计评审稿）

> 日期：2026-11-10。状态：**设计已闭合；2026-11-12 已完成 v1 存储、采集、API/UI 与人工复核实施**。当前每案一次执行；多轮波动、检索语义校准、真实研究质量和生产环境验收不在本次 Mock 工程闭环内。
>
> 本文对应开发路线图 16.10，定义输出、工具、检索和系统四层记录如何关联 Run、GateRun、AgentVersion、版本 diff 与反馈。文中的 JSON 是契约示例，不是已经产生的真实运行证据；本地研究助理仍是 0/5 完整契约通过的失败基线。

## 1. 现状、目标与边界

当前 `Run` 保存输入、文本输出、状态、错误、指标和 Step Trace；`GateRun` 保存自由 JSON `details/diff`，但 diff 只有总通过状态和答案文本差异。`EvalRunner` 的 `LoopResult` 已有 `run_id`，构造 `AgentRunResult` 时却将其置为空字符串，Gate 明细也没有保存实际 Run ID。因此，现有记录不能可靠回答“哪个版本的哪次执行，在哪一层，因为哪段证据失败”。

本设计的目标：

1. 保留原始观察与质量判定的区别，并能定位到一个或多个失败层。
2. 用显式 ID 串联 AgentVersion → Run → Gate evidence → version diff，不按相似输入或输出反推关系。
3. 旧数据库行、旧 API Reader 和旧浏览器继续可读；缺少质量记录不等于质量通过。
4. 固定套件、模型、检索和重复运行条件；条件改变时显示不可直接比较，而不是美化成 `fixed`。
5. 反馈保留来源版本，修复判断关联一次明确的 Gate diff 和人工复核。

非目标：不在 16.10 实施数据库迁移、检索埋点、评分器、前端页面或真实 Provider 验收；不修改 Agent Definition envelope；不把 Memory 查询冒充 KnowledgeBase 检索；不将单次 Mock/Provider 成功解释为发布质量通过。

## 2. 核心决策与关系

采用**增量 JSON-Text 列 + 显式证据引用**，与当前 SQLite/PostgreSQL 兼容方式一致。新记录写 `quality_record_version: 1`；迁移前记录的所有质量列为 `NULL`，不做批量回填。

```text
AgentVersion 1 ── * Run 1 ── 1 answer/tool/retrieval/execution output
     │                  │
     │                  └── 0..1 Feedback ── * FeedbackResolution
     │                                               │
     └── 1 ── * GateRun 1 ── * GateCaseEvidence      │
                       │         │                  │
                       │         └─ run_id ─────────┘
                       ├─ baseline_gate_id（自引用）
                       └─ diff[case_id] ←────────────┘
```

- **Run 是原始观察事实源**：回答、调用、检索和系统事件只记录实际观察；模型自称“有依据”不是独立验证。
- **GateRun 是判定事实源**：断言、分数、失败层和门禁结论属于一次 Gate，不写回并污染可复用的 Run。
- **AgentVersion 是被比较对象**：版本变化本身不是“条件不可比”；套件、输入、评分器、样本数及未声明的运行条件必须保持一致。
- **FeedbackResolution 是追加式人工复核**：原始反馈不可被后续“已修复”状态改写；一次反馈可经历多个候选版本和多次复核。
- `EvalRun/result_case_id` 继续用于评测簿记，但不能替代实际 `Run.id`。

## 3. 通用契约

| 规则 | v1 结论 |
| --- | --- |
| 空值 | 整个质量对象为 `NULL`：legacy/未采集；数组 `[]`：已执行观察但没有项目；`not_evaluated`：没有执行该层断言。三者不可互换。 |
| 状态 | 层判定为 `passed/failed/not_evaluated/not_available/error`；任何必需层的非 `passed` 状态都不得被聚合器静默当成通过。 |
| ID | `run_id`、`agent_version_id`、`gate_run_id` 使用数据库 UUID；`source_id/query_id/call_id/evidence_id` 只在所属记录内要求唯一，跨记录引用必须同时带记录 ID。 |
| 时间 | 新增 JSON 使用 UTC RFC 3339；时长为非负整数毫秒。数据库既有 naive UTC 列不改变语义。 |
| 哈希 | 小写 SHA-256；哈希算法和规范化输入必须固定。哈希用于对账，不代替被授权读取的原文。 |
| 文本位置 | `answer_excerpt` 与 `source_excerpt` 保存精确摘录；如未来增加 offsets，必须统一为 UTF-8 字节半开区间，不能混用 Python 与 JavaScript 字符下标。 |
| 不可变性 | 终态 Run 的四个观察对象不可原地改写；修正采集器应产生新记录版本或补充审计，不重写历史 Gate 判定。 |
| 扩展 | 新 Reader 对同版本未知嵌套键可在类型投影中忽略，但不得重写原始 JSON；高于 1 的 `quality_record_version` 标记 `unsupported_version`，禁止按 v1 解释。 |

## 4. Run v1：四层原始观察

Run 新增五列：`quality_record_version`、`answer_output`、`tool_output`、`retrieval_output`、`execution_output`。完整示例见 [run-v1.json](quality_model/run-v1.json)，旧行示例见 [legacy-run.json](quality_model/legacy-run.json)。

### 4.1 `answer_output`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `answer` | `string/null` | 最终回答原文；有文本时必须与既有 `Run.output` 完全一致。 |
| `answer_status` | enum | `answered/insufficient_evidence/refused/not_produced/unknown`；这是回答处置状态，不是质量通过状态。 |
| `evidence` | array | 回答中实际出现的引用及其来源关联；空数组不自动等于断言失败，判定由 Gate 给出。 |

每个 evidence 项包含 `evidence_id`、`source_id`、`citation_text`、`answer_excerpt`、`source_excerpt` 和 `support_status`。Run v1 的 `support_status` 固定为 `not_checked`，只表示尚未独立评价；`supported/partial/unsupported` 是 Gate 的 citation-support assertion 结论，保存在 assertion 的 status/reason/details 中，不写回 Run。`source_id` 必须存在于同一 Run 的 `retrieval_output.sources`，且至少在一个 query 中 `injected=true`，禁止引用未注入或不可见的资料来伪装支撑。

### 4.2 `tool_output`

顶层固定为 `calls` 与 `results` 两个数组，通过 `call_id` 一对一或一对零关联；运行中尚未返回的 call 可以没有 result。

`calls[]` 最少包含：

- `call_id`：Provider/Loop 调用 ID；若 Provider 未给出，由 Loop 生成并保留映射。
- `call_step_id`、`name`、`arguments`、`risk`、`requested_at`。
- `approval_id`：需要审批时指向既有 ApprovalRequest；无审批为 `null`，不能用空字符串伪装。
- `definition_version`：可获得的工具实现/Schema 版本；未知时为 `null`，不根据名称猜测。

`results[]` 最少包含 `call_id`、`result_step_id`、`status`、`output`、`error`、`duration_ms`、`finished_at`。状态为 `success/error/cancelled/timeout/denied`。`error` 使用 `{category, message, retryable}`；`output` 为脱敏后的结构化结果或文本。调用参数、结果和异常都不得包含秘密值；被删除的字段用 `{redacted: true, paths: [...]}` 描述路径，不记录秘密哈希。

### 4.3 `retrieval_output`

顶层固定为 `queries` 与 `sources`。查询保存排序观察，来源保存身份和内容定位，避免多查询共享同一 source 时rank/score 相互覆盖。

`queries[]`：

| 字段 | 含义 |
| --- | --- |
| `query_id/text/knowledge_base_ids` | 实际查询文本及经权限解析后的 KB 范围；文本敏感时按保留策略存摘录或为空，并记录保留原因。 |
| `embedding` | 实际 `provider/model/dimensions`；当前 hashing 必须写 `hashing`，不得写成 bge-m3。 |
| `index` | `snapshot_id/algorithm/corpus_manifest_sha256`；内存检索也必须如实标识，不能虚构持久向量库。 |
| `top_k/filters` | 实际召回参数与过滤条件，不含秘密。 |
| `candidates[]` | 每项为 `source_id/rank/score/injected`；rank 从 1 开始，`injected` 表示确实进入模型上下文。 |
| `duration_ms/status/error` | `completed/error/cancelled` 及结构化错误；无命中是 `completed + candidates: []`，不是检索通过。 |

`sources[]` 包含 `source_id`、`knowledge_base_id/document_id/chunk_id`、`document_sha256/chunk_sha256`、`locator`、`source_excerpt/excerpt_truncated`。`locator` 至少保留 filename 与 section；可获得时扩展页码或字符范围。引用内容必须来自当时注入的 chunk，后续文档修改不得改变旧记录中的哈希和定位。

### 4.4 `execution_output`

顶层固定为 `events`、`metrics`、`error`、`result`：

- `events[]`：`event_id/step_id/sequence/kind/status/parent_event_id`，按 `sequence` 唯一排序。Step 已存在时保存 `step_id`；新型 retrieval/system 事件可以暂时没有 Step ID，但必须可追溯到执行器。
- `metrics`：复制终态 Run 的 tokens、duration、step_count，并增加实际 tool/retrieval 次数。聚合指标不能替代逐次记录。
- `error`：`null` 或 `{category,message,retryable,step_id}`；分类至少覆盖 `provider/timeout/permission/budget/cancelled/tool/retrieval/internal`。保留既有 `Run.error` 文本以兼容旧 Reader。
- `result`：`status/started_at/finished_at/termination_reason/record_complete`。进程崩溃或采集中断时 `record_complete=false`，不得根据 HTTP 200 或 Provider 返回推断系统层质量通过。

### 4.5 写入时序与一致性

1. 创建 Run 时写版本 1 和初始 execution result；其他层可以是空观察或 `null`（尚未采集），运行状态必须能区分二者。
2. AgentLoop 在事件发生时追加工具、检索和系统观察；检索不得事后从 Prompt 文本猜测。
3. 终态在同一事务中写 `Run.output/status/error`、answer 和最终 execution result；`answer_output.answer` 与 `Run.output` 不一致视为记录错误。
4. Gate 通过 `LoopResult.run_id` 读取已提交 Run。当前 EvalRunner 必须先把该 ID 传入 `CaseEvalResult`，不能用输入文本重新查询“最像的一次 Run”。
5. 暂停、取消、超时和部分 Trace 都保留；只有完整终态记录才可参与要求完整证据的门禁。

## 5. GateRun v1：条件、证据和判定

GateRun 新增 `quality_record_version`、`conditions`、`evidence` 三个可空 JSON/整数列。既有 `details`、`diff`、`status/pass_rate/threshold/failed_case_ids` 保持原字段和语义，旧 UI 不需要理解新对象。

完整示例见 [gate-v1.json](quality_model/gate-v1.json)。

### 5.1 `conditions`：触发时固定比较条件

- `suite`：固定有序 case IDs、每案实际 input/expected/metadata 及其哈希、scorer 名称/版本/实际配置及配置哈希、case 阈值和执行时 Gate 阈值。执行器只读此自包含快照，不再读取执行期间被编辑的 Suite 当前值；快照内容按质量证据权限保护。
- `execution`：AgentVersion、公开 Definition 哈希、实际 Provider/model/model digest、温度、seed（不支持则为 `null`）、每案 run_count 和工具定义哈希。秘密值及明文 secret refs 不进入 conditions；未知 digest 为 `null`，要求权重一致的比较因此为证据不足，不能用模型别名冒充 digest。
- `retrieval`：实际 embedding、index snapshot、corpus manifest 和 top_k；未启用检索时为 `null`，不能沿用 Agent 配置声称发生过检索。
- `comparison.allowed_changes`：默认只允许 AgentVersion 和其 Definition 哈希变化；模型比较等专项任务必须显式声明其他自变量。
- `comparison.required_invariants`：默认固定 case 快照、scorer、run_count、Provider/model/digest、解码条件、工具定义和 retrieval 条件。任何未声明变化进入 diff 的 `changed_conditions`。

Gate 阈值只影响套件级决策，不篡改单案原始 assertion/score。显式 reevaluate 可以继续更新既有 `GateRun.threshold/status`，但不得改写 `conditions`、原始 evidence 或历史 diff。

### 5.2 `evidence[]`：每案、每次 attempt 的层判定

每项使用 `(gate_run_id, case_id, attempt)` 定位，并包含：

- `evidence_id/case_id/result_case_id/attempt/run_id/agent_version_id`；attempt 从 1 开始并在一个 case 内唯一。
- `layers.output/tool/retrieval/system`，分别读取 Run 的 answer/tool/retrieval/execution 对象。
- 每层 `status + assertions[]`；assertion 保存稳定 `assertion_id/kind/status/score/reason/subject`。
- `subject` 至少包含 `run_id + JSON Pointer`，可再带 evidence/query/call/source ID。Pointer 必须能解析到该 Run 的原始观察。
- `passed/failed_layers/error` 为聚合结果。必需层 `not_available/error` 时不能记为 passed；非必需层 `not_evaluated` 必须明确展示。

确定性断言记录规则版本；语义/LLM 评审还必须记录评审模型、rubric、原始理由和人工校准集引用。模型自评不能成为关键安全场景的唯一发布依据。重复运行保存每个 attempt，不用平均分覆盖原始失败；flaky 标记和样本不足属于后续报告逻辑，不得从分母暗删。

### 5.3 `details` 与 `diff` 的增量扩展

`details[]` 保留现有 `case_id/result_case_id/input/expected/output/score/passed/error/duration_ms/tokens`，新增可选 `run_ids/attempt_count/failed_layers`。新 UI 以 evidence 为准，旧 UI 继续读原字段。

`diff[]` 保留 `case_id/change/previous_passed/current_passed/answer_diff`，新增：

| 字段 | 含义 |
| --- | --- |
| `previous_agent_version_id/current_agent_version_id` | 两侧不可变版本；当前侧必须等于 GateRun.agent_version_id，上一侧必须等于 baseline Gate 的版本。 |
| `previous_run_ids/current_run_ids` | 两侧每案全部 attempt 的有序 Run 引用；新增/移除用例允许一侧为空。 |
| `previous_evidence_ids/current_evidence_ids` | 对应 Gate evidence；legacy 侧没有证据时为空并影响可比性。 |
| `layer_changes[]` | 每层 `new_failure/fixed/unchanged/new_case/removed/not_evaluated/not_comparable`；不能只给总分变化。 |
| `comparison_status` | `comparable/conditions_changed/insufficient_evidence/no_baseline`。 |
| `changed_conditions` | 变化条件的 JSON Pointer 列表；空数组才表示声明的不变量全部一致。 |
| `previous_conditions_sha256/current_conditions_sha256` | 两侧 conditions 规范化后的对账哈希。 |

旧 `change` 继续按总 case pass/fail 计算，避免破坏旧 Reader；`comparison_status != comparable` 时，新 UI 不得把它单独解释为“质量已修复/已回归”，而应显示条件变化或证据不足。`layer_changes` 使用 `not_comparable`，不能伪装成 `unchanged`。没有 baseline 时沿用 `new_case`，同时标 `no_baseline`。

示例中的 baseline 刻意使用 legacy Run：旧 `change=new_failure` 仍可供旧 UI 展示，但 `previous_evidence_ids=[]`、`comparison_status=insufficient_evidence`，四层变化全部是 `not_comparable`。当前 Gate 自身仍能判定 output/retrieval failed；不可比较不等于当前 case 通过。

## 6. Feedback 与版本 diff

新增 `Feedback.agent_version_id`，在创建反馈时只复制 `Run.agent_version_id`；若旧 Run 没有版本，保持 `NULL`。现有按时间猜测最近版本的 fallback 只能留在旧 `context_snapshot` 中并标为推断，不能写入新的精确外键。

新增追加式 `FeedbackResolution` 表，示例见 [feedback-v1.json](quality_model/feedback-v1.json)：

| 字段 | 约束 |
| --- | --- |
| `id/feedback_id` | UUID；feedback_id 外键级联删除。 |
| `gate_run_id/case_id` | 指向候选 GateRun 及其 diff case；GateRun.baseline_gate_id 确定比较基线。 |
| `verdict` | `needs_review/confirmed_fixed/not_fixed/conditions_changed`；只有授权人工复核能写 `confirmed_fixed`。 |
| `note/created_by/created_at` | 复核理由、人员和时间；追加记录，不覆盖历史。 |

创建 resolution 时必须校验 Feedback、Run、两个 AgentVersion、GateRun 和 case 同属一个 Agent/团队，且 diff 中确有该 case。`confirmed_fixed` 还要求 comparable diff、对应反馈用例、当前层断言通过及人工理由；Gate 自动通过不能自动写此 verdict。反馈转 EvalCase 仍只是待回归样本，不代表已修复。

## 7. 迁移设计（后续 0012，不在本次实施）

| 对象 | 增量字段/表 | 数据库规则 |
| --- | --- | --- |
| `agent_runs` | 上述五个质量列 | version 为 nullable Integer；四个 output 为 nullable Text，内部为 JSON。无 server default，不批量更新旧行。 |
| `gate_runs` | `quality_record_version/conditions/evidence` | 同样 nullable；旧 `details/diff` 不动。 |
| `feedback` | `agent_version_id` | nullable UUID FK → AgentVersion，`ON DELETE SET NULL`，加普通索引；不按时间回填。 |
| `feedback_resolutions` | 第 6 节字段 | 新表；feedback 外键 CASCADE，Gate 外键 CASCADE；索引 `(feedback_id, created_at)`、`(gate_run_id, case_id)`，case_id 不对可变 EvalCase 强行加 FK。 |

实施时使用 Alembic `batch_alter_table` 保持 SQLite 支持，并将新 JSON 字段注册到现有 `_JSON_TEXT_FIELDS`。所有新列保持 nullable，确保迁移后的旧二进制仍可 INSERT/SELECT。Run 目录继续使用原 `RunSummary` 字段且通过 `load_only/deferred` 避免读取大 JSON；Run 详情才增加五个可选字段。Gate 历史列表不新增 evidence/conditions，详情接口才返回；既有列表字段不得借性能优化之名删除。

### 7.1 Expand / 混合版本 / 回滚

1. 先执行 additive migration，再发布能读写 v1 的代码；旧代码在混合窗口继续写全 `NULL` 质量列。
2. 新 Reader 将全 `NULL` 标为 `legacy_unavailable`，保留原文/Trace，不合成空对象或 passed。新写入的完整终态必须有四个对象；缺层时 `record_complete=false`。
3. 回滚应用代码时，旧 RunOut/浏览器忽略新增响应字段，数据库新列仍保留；禁止用 DROP COLUMN 作为兼容回滚。
4. Alembic downgrade 仅用于尚未写入新数据的开发环境或明确批准的数据删除。生产 downgrade 前必须备份并声明质量证据会丢失。
5. 高于 v1 的记录只能由识别该版本的 Reader 解释；旧质量 UI 显示“不支持的新记录版本”，同时仍展示 legacy output/Trace。

### 7.2 Reader 策略矩阵

| Reader / 输入 | 行为 |
| --- | --- |
| 迁移前 RunOut + 新响应字段 | 保持 Pydantic 默认 `extra=ignore`，忽略质量字段并返回旧模型；2026-11-10 直接探针已确认此现状。 |
| 旧 TypeScript/UI + 新响应字段 | 结构化 JSON 的额外键不参与渲染；新 TS 字段全部 optional/nullable，旧记录不伪造默认值。 |
| 新 Run detail + legacy DB 行 | 返回五个 nullable 字段及原字段；目录 RunSummary 完全不变。 |
| 新 Gate Reader + 旧 details/diff | 原样返回；新增 `quality_record_version/conditions/evidence=null`，层信息显示 unavailable。 |
| v1 质量 Reader + 同版本未知嵌套字段 | 类型投影可忽略未知键，原始 JSON 只读保留；已知字段缺失、类型错误或引用悬空为记录错误，不默认通过。 |
| v1 质量 Reader + version > 1 | 返回原始 legacy 字段并标 `unsupported_version`，不按 v1 丢弃字段后评分。 |
| 客户端写入 | 四个质量对象由服务端生成；复用既有宽松 Create DTO 时也必须忽略客户端伪造字段，绝不能直接持久化。 |

此策略只针对质量记录，不改变 Agent envelope 2 对未知 Schema 版本的严格拒绝规则。

## 8. API、权限和数据保护

### 8.1 API 形态

- `GET /agents/{agent_id}/runs`：响应和查询列保持旧摘要；不返回四层对象。
- `GET /agents/{agent_id}/runs/{run_id}`：增量返回五个可空质量字段；WebSocket `run.complete` 保持旧小摘要，终端后由详情接口取质量记录。
- Gate 列表保持现有响应；Gate 详情增加 `quality_record_version/conditions/evidence`，`details/diff` 只做上述增量扩展。
- Feedback 响应增加可空 `agent_version_id`；resolution 通过独立受权限保护的详情/创建接口访问，不塞入原 `context_snapshot`。
- Studio 深链接使用 `agent_version_id + run_id`；Gate 深链接使用 `gate_run_id + case_id + attempt`，失败跳回时保留输入和版本，但不复制或修改旧 Run。

所有质量字段沿用 Run/Gate/Feedback 的团队与 Agent RBAC。跨团队 run、KB、Gate、版本和 feedback 链接统一按不可见资源处理，不能通过验证错误泄露名称或内容。Delivery token 不获得质量证据管理权限。

### 8.2 脱敏、保留和大小限制

1. 工具 arguments/results、Provider error、检索 filters 和反馈快照统一走递归秘密脱敏；秘密不写原文，也不写可暴力反推的低熵哈希。
2. Prompt injection 文本是不可信数据，只用 JSON 转义和前端文本节点展示；不得渲染为 HTML 或作为评分器系统指令。
3. 原始 answer 与检索摘录可能含私人知识，导出和日志不默认包含；访问、导出和 resolution 都写审计。
4. 实施时配置单 Run/单 query/单 call 的字节上限和数组上限；截断必须设置 `record_complete=false` 或具体 `*_truncated=true`，不能无提示丢证据。
5. Run 删除按现有级联清理四层记录和反馈链接；文档后续修改不修改历史哈希。保留期、导出和删除策略属于后续部署配置。

## 9. 实施切片与验收矩阵

### 9.1 推荐实施顺序

1. **纯契约与 fixtures**：Pydantic/TS v1 类型、JSON Pointer 解析、状态聚合、旧/新/未知版本 Reader fixtures。
2. **0012 additive migration**：真实 SQLite 旧库升级、混合版本写入和索引检查；PostgreSQL 在目标环境另验。
3. **Run 采集**：AgentLoop 写 answer/tool/execution；KnowledgeBase 检索埋点写 query/source。没有检索时写已观察的空 queries/sources，不能生成虚假的来源。
4. **Gate 证据链**：EvalRunner 传播 `LoopResult.run_id`，Gate 固定 conditions，保存 attempts/evidence，并增量构建 layer diff。
5. **Feedback/API/UI**：精确版本外键、追加 resolution、Studio/Quality/Operations 深链接和旧记录提示。
6. **独立真实验收**：先 Mock 工程链路，再真实 Provider、多轮重复、人审和发布；两种证据分文件保存。

每片独立回滚代码，不能先改 UI 再用客户端提交四层 JSON 伪造后端证据。

### 9.2 单元与契约测试

- v1 四个对象、Unicode、空数组、null、截断和错误状态 round-trip；answer 与 Run.output 不一致、重复 ID、悬空 source/call/step/Pointer 均拒绝。
- `answered/insufficient_evidence/refused` 与 Gate pass/fail 解耦；Provider success 只能满足对应系统断言，不能覆盖 output/retrieval 失败。
- 必需层 missing/not_available/error 不通过；非必需层 not_evaluated 明确展示；空检索结果不等于检索质量通过。
- 多次 attempt 全量保留，顺序稳定；pass rate、波动、样本数不从 flaky case 暗删。
- 旧 RunOut unknown-field fixture、旧 TS JSON fixture、旧 details/diff fixture 保持原字段；version 2 不走 v1 评分。
- 客户端提交伪造 quality objects 不入库；所有跨团队引用和秘密递归 fixtures 拒绝或脱敏。

### 9.3 迁移与 API 测试

- 在 0011 数据库预置 legacy Run/Gate/Feedback，再升级 0012：旧文本、Trace、details/diff 完全不变，新列全 NULL，Feedback 不按时间猜版本。
- 旧二进制在升级后插入/读取，新二进制读取旧行，新二进制写 v1 后旧二进制读取，三种混合场景均通过。
- upgrade/head/current/idempotent/round-trip 保留；downgrade 数据丢失警告有测试和发布说明。
- Run 目录响应键与 0011 快照一致，SQL 不加载四层大字段；Run 详情和 Gate 详情才返回质量对象。
- Gate 条件在创建后不受 Suite 当前编辑影响；模型、corpus、run_count 或 scorer 未声明变化时 diff 标 `conditions_changed`。
- reevaluate 不改 conditions/evidence/diff；bypass 不把层失败改成 passed；FeedbackResolution 不能被自动 Gate 写成 confirmed_fixed。

### 9.4 E2E 与浏览器准入（实施完成后执行）

使用独立数据库和端口，先从真实 0011 fixture 创建旧 Record，再升级并运行两个 Mock AgentVersion：

1. 旧 Run/Gate/Feedback 原文、Trace、总 diff 和目录仍可打开，显示“无四层记录”，不是全绿；刷新后保持。
2. 新 Run 经真实 AgentLoop 产生四层对象，至少分别制造 output、tool、retrieval、system 失败，Gate 展示具体失败层和原始证据。
3. 从 Gate case 跳到对应 Run/AgentVersion，再回 Studio；所有 ID、attempt 和 JSON Pointer 对账，不能跳到相似输入的另一条 Run。
4. 同输入双版本比较显示 `new_failure/fixed/unchanged/new_case/removed` 和层变化；改变 corpus/run_count 后显示条件不可比。
5. 负面反馈固定来源版本，人工确认后才显示 confirmed_fixed；新 resolution 不改写旧反馈或旧 Gate。
6. 收集 console、page error、失败请求、Run/Gate/Version IDs 和截图；任何未解释错误都阻止浏览器验收。

Mock 浏览器只证明工程链路。真实 Provider 仍需按本地研究助理专项计划完成至少三轮、十案、人审、门禁和交付；不得将上述工程验收替代旗舰质量接受。

## 10. 本地研究助理失败基线的映射

| 已观察问题 | Run 原始观察 | Gate 失败层 | 不得作出的推断 |
| --- | --- | --- | --- |
| 回答缺引用 | `answer_output.evidence=[]` | output 引用断言 failed；若检索实际成功，retrieval 可独立 passed | 模型或 Provider 整体不可用。 |
| 弱相关引用 | candidates/sources 保存实际 rank、score、chunk 和注入状态 | retrieval 相关性 failed；output support 也可 failed | 有 `[1]` 就等于有证据支撑。 |
| 回答不完整 | answer 原文保留 | output 必要内容 failed | Provider 完成就等于答案通过。 |
| Provider 协议成功 | execution result/status/error 如实记录 | system 的对应断言可 passed | 四层总分通过或可发布。 |

现有五条 direct Ollama probe 不经过 AgentLoop，也没有这些数据库列和检索 Trace，**不能事后伪造或批量回填为 Run v1**。只有后续按相同 Definition A、模型 digest、语料 manifest、检索条件和运行次数重新执行，才能生成可比较的新证据；Prompt B 仍必须使用新的不可变版本。

## 11. 16.10 完成定义与未闭合项

16.10 设计完成只表示：四层字段、空值/状态、ID 关联、Gate 条件与逐 attempt 判定、增量 diff、反馈复核、0012 迁移、Reader 策略和 E2E/浏览器矩阵均已无歧义，并有可解析示例通过静态交叉校验。

2026-11-10 的纯契约切片已落地：`backend/app/quality` 提供 v1 Pydantic 模型、RFC 6901 Pointer、层聚合、版本 Reader 和引用校验；`backend/app/api/schemas.py` 新增尚未接线的详情 `RunDetailOut`，旧 `RunOut/RunSummary` 不变；`frontend/src/api/quality.ts` 与 `quality.fixtures.ts` 提供 TS 类型和静态兼容 fixtures。后端测试直接读取本节四份 JSON，TS fixtures 也引用同一源文件。它不包含数据库写入、真实采集或评分器实现。

设计评审时，以下曾是后续实现债务：实际 0012 文件、ORM 与 API/执行器接线、AgentLoop/检索埋点、EvalRunner Run ID 传播、Gate conditions/evidence writer、反馈 resolution API、四层 UI、迁移/E2E/浏览器实测，以及真实 Provider 的质量与人工验收。16.9 旗舰包不因此解除质量拒绝；第 8 步整体完成条件和第 9.2 步旗舰验收也不因本设计或纯契约切片而完成。

**2026-11-12 实施补记：**除真实 Provider 研究质量和多轮波动统计外，上述 v1 工程项已落地。0012 head 为 `b72f41a9c012`；0011 旧行、旧摘要和旧字段插入兼容有迁移测试；Run 采集来自真实 Step/检索注入，Gate 保存冻结条件、逐案证据与层级 diff，反馈 resolution 走权限校验和人工确认。全量后端 438 项通过，真实 Edge 闭环及最终媒体验收记录见开发基线。检索相关性/引用支持度仍需专项 scorer 与人工校准，不能由“检索完成”断言替代。

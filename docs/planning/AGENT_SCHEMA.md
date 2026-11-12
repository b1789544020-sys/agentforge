# Agent Schema v1 设计（评审稿）

> 日期：2026-11-10。状态：**v1 设计评审、16.8 后端前置契约及最小单 Agent Studio 均已闭合；增强型工坊仍属后续范围**。
>
> 本文对应开发路线图 16.7，范围是统一 Agent 定义、版本、导入导出和未来 Studio 的边界。公开脱敏、掩码秘密再导入、Prompt 固定和知识绑定快照规则见第 5–7 节。第 12 节的后端契约前置项已实现并通过两套 Python 全量回归；前端质量与隔离浏览器证据及后续 Studio 边界以开发基线为准。

## 1. 现状与问题

当前系统已经有可运行部件，但它们不是同一个定义模型：

| 部件 | 当前事实 | 需要解决的问题 |
| --- | --- | --- |
| `Agent` ORM | 名称、描述、Prompt、Provider、模型、温度、步数、工具名和任意 JSON `config` 分列保存 | `config` 同时承载运行参数、资源 ID、Prompt 引用和密钥，边界不清 |
| `AgentCreate` / `AgentUpdate` / `AgentOut` | API 使用扁平字段，响应会递归脱敏 | 缺少统一约束、乐观并发和可移植资源表达 |
| `AgentVersion` | 每次创建或更新追加完整 JSON 快照，运行时可从快照重建 | 快照无 Schema 版本；知识绑定在快照外；未固定 Prompt 版本时运行会取最新模板 |
| 导入 / 导出 | envelope `version=1`、`kind=agentforge.agent`，支持 JSON/YAML；导出脱敏 | 旧解析器会丢弃未知字段；脱敏值可能被当成真实密钥再次导入；本地资源 ID 不可移植 |
| 知识绑定 | `KnowledgeBinding` 独立保存；交付时读取当前绑定 | 修改绑定不会改变旧版本内容，旧版本交付可能使用新绑定 |
| API 读取 | 普通 Agent 与模板响应会脱敏 | `gates.version_out` 和交付详情的快照路径未统一脱敏，实施时必须先消除该差异 |
| Run | 创建时记录最新 `agent_version_id` | 普通运行请求不能显式选择旧版本；还需保证执行对象与记录的版本一致，才能支持同输入版本比较 |

另外，模型权重、Provider 服务、知识库文档内容、工具实现和服务端依赖都可能变化。**冻结 Agent JSON 不等于比特级复现**，文档和 UI 不得作此宣称。

## 2. 目标与非目标

### 目标

1. 为 CRUD、不可变版本、JSON/YAML 包和 Studio 提供一个 canonical Agent Definition。
2. 明确区分公开可移植定义、服务端运行快照和一次性秘密输入。
3. 新服务无损读取现有包与快照；不让旧服务静默接收并丢弃新资源字段。
4. 对资源解析、权限、脱敏、冲突和迁移提供确定性规则及错误报告。
5. 为 16.8 的“配置 → 保存版本 → 指定版本运行 → 同输入比较”提供接口前置条件。

### 非目标

- 不在 v1 引入多 Agent、可视化工作流 DSL、自动 Prompt 修复或模型路由。
- 不把 Memory 内容、知识文档、向量、评测数据或部署凭据打包进 Agent 定义。
- 不承诺跨服务器自动推断同名知识库就是同一资源，也不自动复制第三方 Provider 密钥。
- 不在本轮重写 ORM；优先使用纯 Python/Pydantic 适配层和增量 JSON 字段。

## 3. 两层版本号与兼容策略

采用两个正交版本：

- **Package envelope**：当前为 `1`，新 canonical 包使用 **`2`**；标识 `agents` 列表、包级元数据和序列化外壳。
- **Agent Definition**：从 **`schema_version: 1`** 开始；标识每个 Agent 条目的字段语义。

新导出使用 envelope 2 是有意的安全边界：旧服务只接受 envelope 1，因此会明确拒绝新包，而不是忽略 `resources` / `secret_refs` 后生成缺字段的 Agent。新服务必须：

| 输入 | 行为 |
| --- | --- |
| envelope 1，无 `schema_version` | 走 legacy 归一化，保留现有默认值、字段和冲突策略 |
| envelope 2，条目 `schema_version: 1` | 走本文严格校验、资源预检和秘密规则 |
| 更高 envelope 或 Schema 版本 | 整个导入失败，不部分导入、不降级丢字段 |
| JSON 或 YAML | 解析后进入完全相同的校验流程；YAML 只允许安全解析 |

旧 `AgentVersion.snapshot` 没有 `schema_version` 时按 legacy snapshot 读取，保持原重建路径；只有新保存的快照写入 `schema_version: 1`。不做批量数据库重写。

## 4. Canonical Agent Definition v1

envelope 2 的完整示例：

```json
{
  "version": 2,
  "kind": "agentforge.agent",
  "exported_at": "2026-11-10T15:00:00Z",
  "agents": [
    {
      "schema_version": 1,
      "name": "local-research-assistant",
      "description": "Answer from explicitly selected public documents.",
      "system_prompt": "Cite supplied evidence and state when evidence is insufficient.",
      "provider": "ollama",
      "model": "qwen2.5:3b-cpu",
      "temperature": 0.2,
      "max_steps": 8,
      "tools": [],
      "config": {
        "base_url": "http://127.0.0.1:11434/v1",
        "max_context_tokens": 8000,
        "prompt_variables": {"audience": "developer"}
      },
      "resources": {
        "prompt": {
          "name": "research-system",
          "version": 3
        },
        "knowledge_bases": [
          {"ref": "kb-1", "name": "AgentForge public docs"}
        ]
      },
      "secret_refs": [
        {"path": "/config/api_key", "ref": "local-provider", "required": false}
      ],
      "extensions": {}
    }
  ]
}
```

`exported_at` 只是包元数据，不参与 Agent 语义 diff。`name` 在当前冲突策略下仍是导入匹配键，不是跨系统全局身份。

### 4.1 核心字段

| 字段 | v1 规则 |
| --- | --- |
| `name` | 去除首尾空白后长度 1–128；禁止仅空白 |
| `description` / `system_prompt` | 字符串或 `null`；不擅自截断已有文本 |
| `provider` / `model` | 非空字符串；Provider 是否受支持由运行前检查单独报告，不在纯 Schema 中硬编码厂商列表 |
| `temperature` | 有限数值，禁止 NaN/Infinity；v1 先保留现有 0–2 之外的读取兼容，Studio 写入采用 0–2 |
| `max_steps` | Studio 写入为 1–100 的整数；旧快照中的其他值仍可读取并标记校验警告 |
| `tools` | 有序、非空的唯一名称列表；不静默去重或丢弃未知工具 |
| `config` | JSON 对象；已知键按运行语义校验，未知非敏感扩展必须保留 |
| `resources` | 可移植资源引用，不保存其他租户的本地 ID |
| `secret_refs` | 秘密所在 JSON Pointer、包内逻辑 ref 和是否必需；不包含秘密值 |
| `extensions` | 可选对象，供后续声明式扩展；v1 运行器忽略前必须保留并提示，不能宣称已执行 |

legacy 导入沿用当时的默认值。v1 canonical 导出始终写全核心默认值，避免不同 Reader 自行猜测。`temperature=0`、`max_steps` 等 falsy 值不能用 `or 默认值` 处理。

### 4.2 `config` 保留键的归一化

- `base_url` / `endpoint`、`max_context_tokens`、`mock_reply` / `mock_replies` 和 `prompt_variables` 仍属运行配置。
- legacy `prompt_template_id` / `prompt_version` 归一化为 `resources.prompt`；本地 ID 只留在服务端运行表示。`resources.prompt` 存在时沿用当前“模板覆盖裸 Prompt”的运行语义，`system_prompt` 是无模板绑定时的 fallback，不允许两种来源顺序含糊。
- legacy `knowledge_base_ids` 归一化为 `resources.knowledge_bases`；本地 ID 不进入可移植包。
- `memory` 暂作为未知配置保留。当前快照虽复制该字段，但现有主循环没有形成完整、可验证的 Memory 配置契约，v1 不虚构其语义。
- 任意嵌套位置命中现有敏感键规则的值都进入秘密处理，而不是只处理顶层 `api_key`。

## 5. 秘密与脱敏契约

### 5.1 三种表示必须分开

1. **公开 Definition**：敏感值从 `config` 移除，只在 `secret_refs` 留下路径、逻辑 ref 和 required 标记。
2. **内部 Runtime Snapshot**：为兼容当前运行器可保存 Fernet 密文，但绝不能原样从版本、交付、模板或日志 API 返回。
3. **一次性秘密输入**：只存在于创建/更新请求的 `config` 敏感键中，经 `protect_config` 后落库；不进入导出包、审计详情、浏览器持久化或 diff。

`secret_refs.path` 使用 RFC 6901 JSON Pointer，数组下标必须精确保留。公开 diff 只显示“未设置 / 已设置 / 已替换”，不得显示明文、密文或可被当作密文指纹的比较值。

### 5.2 脱敏包与秘密输入状态机

旧 envelope 1 使用现有敏感键规则定位秘密；envelope 2 同时使用敏感键和 `secret_refs.path`，两者冲突时导入失败，不能猜测。对递归找到的每个敏感路径，秘密合并必须遵循下表，而不是再次调用通用加密函数处理 `********`：

| 输入状态 | create | overwrite | 说明 |
| --- | --- | --- | --- |
| 路径未出现 | 未设置 | 保留原值 | 非敏感字段仍按各自替换语义处理 |
| `********` | 未设置 | 保留原值 | 只是兼容标记，绝不加密成伪密钥 |
| `null` 或空字符串 | 未设置 | 显式清除 | required 秘密会在运行前报错 |
| 非空明文 | 加密后写入 | 替换并加密 | 只存在于本次请求受控内存，不进日志/审计 |
| `enc:v1:` 或未知密文前缀 | 整批拒绝 | 整批拒绝 | 包不搬运来源实例密文，避免错误密钥/指纹泄露 |

overwrite 的“保留”按 RFC 6901 指针逐个应用。只有父对象/数组结构兼容、目标路径仍存在且仍指向同一敏感语义时才能继承；数组缩短、元素重排导致指针语义不确定、父节点类型变化或敏感键改变时返回带路径的 `422 secret_path_conflict`，整批不写库。禁止仅按数组新下标把旧秘密套到另一个条目。

新 canonical 包的秘密通过一次性 `secret_inputs` 请求字段按包内 ref 提供；该字段与 Definition 分模型校验，绝不回显，也不以明文进入版本快照。只有派生出的 Fernet 密文可以按第 5.1 节进入内部运行表示。缺少 required 秘密时允许完成定义导入，但 dry-run 和运行前检查必须返回明确的 `missing_secrets`；Studio 不得静默降级成 Mock 后宣称真实 Provider 成功。

### 5.3 统一公开投影与出口边界

实施前先修复现有出口不一致，且只保留两条明确的公开表示：

1. canonical `public_definition(snapshot)`：递归提取敏感值，公开 `config` 删除这些值，以 `secret_refs` 表达路径、逻辑 ref 和 required 标记；设置状态由 preflight 的 `missing_secrets` 等结果单独表达；
2. legacy `public_legacy_dto(snapshot)`：保留现有 DTO 形状，但对完整对象递归执行只读脱敏，敏感值只能显示 `********`。

普通 Agent CRUD、AgentVersion 列表/详情、`gates.version_out`、模板响应、Deployment 详情/RuntimeSpec、公开交付配置、导入 dry-run 和版本 diff 必须经过其中一个投影及最后的递归 `assert_public_safe` 边界。禁止任何路由直接返回 ORM `config`、`AgentVersion.snapshot` 或内部 RuntimeSpec。读取路径不得调用 `protect_config`，避免把掩码再次加密。

`assert_public_safe` 同时检查敏感键和值：任意嵌套对象/数组都不得出现明文 fixture、`enc:v1:`、未知密文前缀或秘密哈希；diff 只能返回 `unset/set/replaced`，不能比较密文。审计、异常响应和日志使用同一秘密提取结果及 `redact_text`，但不得为了脱敏改写运行所用的内部对象。公开投影一律在深拷贝上进行。

测试以参数化方式遍历上述每个出口，递归断言不含明文/密文，并覆盖嵌套数组、null、掩码和中文值；不能只检查顶层 `api_key` 或单个 CRUD 路由。

## 6. 资源引用与迁移

### 6.1 Prompt

- `resources.prompt.name` 是当前 Agent 所属 PromptTemplate 的可移植名称。可编辑 Definition 的 `version` 为正整数或 `null`：`null` 表示保存时解析最新版本；不可变 AgentVersion 的公开 Definition 和 Runtime Snapshot 必须写回解析后的正整数，不能继续写 `latest/null`。
- 导入指定版本时，dry-run 必须确认映射后的目标模板确有该版本且调用者有权访问；解析最新版本与 AgentVersion 写入处于同一保存事务，不能在预检后静默换成另一个版本。
- 保存时固定的是 **PromptVersion 原始模板内容**，不是提前渲染好的最终字符串。Runtime Snapshot 同时保存 `template_id`、解析后的 `version`、原始内容和 `sha256 = SHA256(content.encode("utf-8"))`；哈希用于完整性核对，不是安全签名，也不能替代内容。
- 从该版本运行/交付时只读取快照中的原始内容，并使用同一快照内的 `config.prompt_variables` 在每次运行开始时渲染。新增模板版本、修改绑定或删除模板都不能改变旧 AgentVersion 的 Prompt；模板变量修改仍须通过共享保存服务生成新 AgentVersion。
- `resources.prompt` 存在时，原始模板内容覆盖 `system_prompt`；没有绑定时才使用快照 `system_prompt`。渲染失败返回带模板版本和变量路径的运行前错误，不回退到裸 Prompt 或最新模板。
- legacy 快照没有固定版本或内容时保持旧行为，并在公开验证信息中标记 `unpinned_prompt`，不能伪造已固定状态。

### 6.2 知识库

- 每个包内条目以非空 `ref` 唯一标识；`name` 仅供用户确认，不是自动绑定凭据。`kb-1` 等 ref 只要求在包内稳定唯一，不宣称是跨服务器永久身份。同一包内重复 ref、重复解析后的知识库 ID 或空 ID 均校验失败。
- Runtime Snapshot 的 `knowledge_bases` 按 `ref` 的 Unicode 码点升序排列，再序列化；不得依赖数据库返回顺序。legacy Agent 首次归一化时没有 ref，先按本地知识库 ID 排序再生成 `kb-1…kb-N`，一旦写入版本，该 ref/ID 对应关系不再随当前绑定变化。
- 绑定、解绑和普通 Definition 保存必须共用同一服务及事务：校验团队权限，序列化同一 Agent 的写操作，写入绑定变更，并在同一事务读取和冻结完整绑定集合、追加 AgentVersion 与审计。自动门禁只能指向刚追加的版本，不能读取另一份“当前绑定”构造运行定义。
- 新 AgentVersion 运行与交付只读取该快照的绑定集合，不再追加查询当前 `KnowledgeBinding`。快照引用的知识库后来被删除或失去权限时返回明确的 `resource_unavailable`，不得回退到当前绑定或静默减少检索范围；legacy 快照继续走原兼容路径并显示 `unpinned_knowledge_bindings`。
- 文档和 chunk 当前没有随 AgentVersion 冻结，因此只能宣称“绑定集合固定”，不能宣称检索语料固定。语料版本与检索证据留待旗舰包和四层质量模型处理。

### 6.3 工具

工具名列表随版本冻结。导入预检按目标团队检查工具是否存在、启用及当前用户是否有权使用；未知或未授权工具不得静默删除。工具代码、动态元数据和风险策略并不随 JSON 冻结，应记录服务端构建版本作为环境信息，但不伪装成内容寻址插件包。

### 6.4 跨环境导入

资源映射是显式输入，逻辑形式为：

```json
{
  "knowledge_bases": {"kb-1": "target-kb-uuid"},
  "prompts": {"research-system": "target-template-uuid"}
}
```

- 先提供 `dry_run=true` 预检，返回逐条 `resolved`、`unresolved`、`missing_secrets`、`unsupported_tools` 和权限错误。
- 目标资源必须与 Agent 同属可访问团队；禁止仅凭名称跨团队匹配，也禁止用导入包中的来源 ID 越权探测资源。
- 未解析资源默认整批失败，不产生 Agent、版本或门禁。只有全部预检通过才在一个事务中写入；秘密和映射不得写入审计明细。
- 同实例 `skip` 的既有 Agent 不做任何资源改写；`overwrite` 仍需完整预检，避免先改一半再失败。

实现时保留上述显式映射边界，并处理两个现有 ORM 事实：

- 多 Agent 导出若出现重复知识库/秘密逻辑 ref，包导出器使用 `agent-name:ref` 做确定性命名空间限定（超长值使用稳定哈希截断），不改变单 Agent ref；包内 Agent 名称和最终 ref 仍必须唯一。
- 同名 Prompt 可用 `agent-name:prompt-name` 作为映射键，限定键优先于非限定键。新建 Agent 时，因 `PromptTemplate.agent_id` 必须归属当前 Agent，映射模板作为同团队来源复制（报告 `action=copy`），原模板不移动、不跨 Agent 绑定；overwrite 只接受目标 Agent 自有模板。

## 7. 版本、保存与运行语义

### 7.1 内部快照与公开投影

新 `Runtime Snapshot v1` 在现有完整字段上增加：

```json
{
  "schema_version": 1,
  "resources": {
    "prompt": {"template_id": "local-id", "version": 3, "sha256": "..."},
    "knowledge_bases": [{"ref": "kb-1", "id": "local-id"}]
  },
  "prompt_content": "the immutable raw PromptVersion template used by this version",
  "secret_refs": [{"path": "/config/api_key", "ref": "local-provider", "required": false}]
}
```

`config` 仍是当前 ORM/运行器需要的内部兼容表示。公开接口不得直接返回这个对象，而要经过统一 Definition 投影；旧响应中的 `snapshot.config` 兼容字段也必须脱敏。`memory` 的旧重复副本不参与新快照语义，legacy 读取仍忽略其差异。

### 7.2 保存

- 旧 CRUD 和新 Definition API 必须调用同一个保存服务，避免版本、门禁或审计只走一条路径。
- 每次成功保存仍追加版本，v1 不顺便引入“无变更不生成版本”的行为变化。
- Studio 保存额外携带 `base_version_id`。服务端发现当前最新版本不同则返回 409 和最新版本号，防止两个编辑者静默覆盖。
- 版本号生成、团队归属和权限检查保持在服务端；客户端不能指定 `version_number`、owner 或密文。

### 7.3 指定版本运行

16.8 已为运行请求增加可选 `agent_version_id`，最小 Studio 的两个比较分支均显式提交该字段：

- 缺省保持“当前最新版本”的兼容行为。
- 指定版本时必须校验该版本属于路径中的 Agent，并通过 `agent_from_version` 重建执行对象；`Run.agent_version_id` 必须与实际执行快照相同。
- 运行开始后的并发编辑不能改变该 Run 的定义。
- 同输入比较记录左右两个版本 ID、输入、运行环境及终态。输出差异不能自动归因于 Prompt，因为 Provider、知识语料和工具环境仍可能变化。

## 8. 建议接口与代码边界

### 8.1 API

| 方法与路径 | 用途 |
| --- | --- |
| `GET /agents/{id}/definition` | 返回当前可编辑 canonical Definition、最新版本 ID 和运行前警告 |
| `PUT /agents/{id}/definition` | 校验 Definition 和 `base_version_id`，通过共享保存服务追加版本 |
| `GET /agents/{id}/versions/{version_id}/definition` | 返回该不可变版本的公开 Definition，不返回内部密文快照 |
| `POST /agents/import?dry_run=true` | 预检包、资源映射、权限和缺失秘密，不写数据库 |
| 现有 `POST /agents/import` | 保留 multipart 兼容；接受 envelope 1/2，正式导入沿用预检结果和事务规则 |
| 现有 `POST /agents/export` | 默认生成 envelope 2；如提供 legacy 导出选项，必须明确其资源/Schema 信息损失，不能静默选择 |
| 现有 `POST /agents/{id}/runs` | 增加可选 `agent_version_id`，响应保留实际版本 ID |

所有路由继续同时挂载 `/api` 与 `/api/v1`。Definition 请求中的秘密只走现有 HTTPS/鉴权配置；本机开发不代表公网安全验收。

### 8.2 模块

建议新增 `backend/app/agent/schema.py`，只包含：

- Pydantic Definition、ResourceRef、SecretRef、ImportReport 模型；
- legacy → v1 归一化；
- ORM/Runtime Snapshot ↔ 公开 Definition 的纯函数投影；
- JSON Pointer 秘密提取/清除、稳定语义 diff 和校验错误路径。

数据库查询、团队权限、事务、版本追加、门禁和审计放在服务/API 层，不塞入纯 Schema 模块。现有 `exporter.py` 负责 envelope 与 JSON/YAML，不再独自维护另一份 Agent 字段列表。

前端只生成一套 `AgentDefinition` TypeScript 类型，可以从后端发布的 JSON Schema 生成，也可先手写并加契约测试；禁止长期复制两份字段默认值。旧 `Agent` 类型作为兼容 API DTO 保留，Studio 不直接把它当 canonical 定义。

## 9. 分阶段实施与回滚

1. **契约测试先行**：固定 legacy envelope、legacy snapshot、秘密和跨团队资源 fixtures；先验证当前可兼容读取的字段。
2. **纯模型与公开投影**：接入统一脱敏和 v2 导出/v1+v2 导入，不改变旧 ORM 列。
3. **共享保存服务**：CRUD、导入、知识绑定和 Definition API 共用版本/审计/门禁流程，补乐观并发。
4. **快照资源固定**：固定 Prompt 内容与知识绑定集合，接入指定版本运行；legacy 快照走原路径。
5. **最小 Studio**：只选择一个 Agent，接通左栏 Definition、中栏同输入调试、右栏 Trace、保存版本和双版本比较。

每一步都可独立回滚代码；因为无批量数据库重写，回滚后旧 Reader 仍能读取旧快照。对于已经写入的 v1 快照，回滚版本必须至少保留只读兼容，或在发布前明确禁止回滚到不识别该版本的二进制。不得用数据库脚本删除新字段来“回滚”。

## 10. 验收测试矩阵

### Schema 与包

- 当前 envelope 1 JSON/YAML fixtures 原样可读，默认值与现有导入一致；缺字段、错误类型、NaN、重复工具和未知版本得到带路径的 4xx。
- envelope 2 的中文、Unicode、嵌套扩展及 JSON Pointer 转义可无损 round-trip；`exported_at` 不影响语义 diff。
- 新包被当前旧 Reader 明确拒绝，而不是静默丢资源；新 Reader 对旧包和新包都有测试。
- 批量导入任一条目失败时无部分 Agent、版本、绑定、门禁或审计写入。

### 秘密与权限

- 顶层、嵌套对象和数组中的秘密均不进入导出、CRUD、版本列表/详情、门禁、交付、模板、dry-run、diff、异常或日志；所有出口复用参数化递归断言。
- 旧 `********` 在 overwrite 保留原秘密、create 不生成伪密钥；null/空字符串清除、明文替换、密文拒绝和缺失秘密的运行前错误分别覆盖。
- 数组缩短/重排、父类型改变和敏感键改变返回 `secret_path_conflict` 且整批无写入；`secret_inputs` 不回显、不进入审计或明文快照。
- 非本团队知识库、模板、工具和版本不可通过 ID 探测或绑定；无权限响应不泄露资源名称。
- Studio 过期 `base_version_id` 返回 409，后保存者不能覆盖先保存者。

### 版本与运行

- Prompt 从“跟随最新”保存后固定具体版本、原始 UTF-8 内容和 SHA-256；运行时再用同一快照变量渲染。新增/删除模板版本、解绑或渲染失败均不改变旧 AgentVersion，也不允许静默 fallback。
- 知识绑定变化追加版本；快照按 ref 确定排序且 ref/解析 ID 均唯一。旧版本仍使用旧绑定集合，新版本使用新集合，交付行为一致；删除资源返回 `resource_unavailable`，不回退当前绑定。
- 指定 v1/v2 两个版本以同一输入运行时，Run 记录与实际执行版本一致；运行中再次编辑不影响进行中的 Run。
- legacy snapshot 无 Schema 标记时仍按旧路径重建，并明确显示未固定资源的限制。

### 16.8 浏览器准入

使用一个隔离 Agent 和独立数据库：左栏修改并保存版本 2，中栏以同输入分别运行 v1/v2，右栏展示各自 canonical Trace，刷新后版本和比较结果仍可追溯。收集浏览器错误、失败请求、版本 ID、Run ID 和截图；Mock 只标为工程证据，真实 Provider 另行验收。

## 11. 评审决策点

以下默认值已逐项复核并作为 v1 设计结论；实现如需偏离，必须先重新评审并同步修改示例、迁移规则和测试矩阵：

1. **新包使用 envelope 2 + Definition v1**，而不是向旧 envelope 1 偷加字段；优先防止旧 Reader 数据丢失。
2. **资源显式映射、整批原子导入**，不自动按同名绑定；这会增加迁移步骤，但能避免跨团队和错绑风险。
3. **v1 固定 Prompt 内容和知识绑定集合，不冻结知识语料或工具代码**；UI 必须展示该边界。
4. **Studio 采用 `base_version_id` 乐观并发，Run 增加 `agent_version_id`**；这是可信版本比较的前置条件。
5. **旧 CRUD/包继续可读，新 Studio 只使用 canonical Definition**；不一次性删除旧 DTO 或批量迁移数据库。
6. **统一公开投影和秘密状态机属于安全前置契约**；所有出口递归检查，掩码按 JSON Pointer 保留，结构冲突整批拒绝，不能把局部 `redact_config` 调用当作完整边界。

评审若修改上述任一决策，应同步更新示例、迁移规则与验收矩阵，不能只改文字结论。

## 12. 完成定义

16.7 的“设计”完成仅表示：两层版本兼容、统一公开脱敏、掩码秘密状态机、资源映射、Prompt 原始内容 pin、知识绑定不可变快照和指定版本运行语义均已有无歧义规则及验收矩阵。设计勾选本身不代表实现完成；后端及最小 Studio 的实际实现与验收状态分别见本节下文。

进入 16.8 Studio 实施所需的后端前置项包括：legacy/安全 fixtures、统一公开投影、秘密结构冲突、Prompt pin、知识绑定快照、指定版本运行、envelope 2、当前 Definition GET/PUT、`base_version_id` 乐观并发、显式资源映射和必需秘密阻断。上述项目均已实现；交付前仍须保留完整后端、前端质量入口及隔离浏览器的最新通过证据，不能把后端契约完成等同于 Studio 完成。

**2026-11-10 首批实现状态：** legacy/security fixtures、递归公开投影及版本公开 Definition、掩码继承/未设置报告、legacy envelope 1 的 `dry_run`、秘密结构冲突、Prompt 原文与 SHA-256 pin、排序知识绑定快照、指定版本运行及两套完整后端回归已落地，新增契约测试 13 项。

**2026-11-10 第二批实现状态：** 默认 envelope 2 JSON/YAML 导入导出、显式 legacy 导出警告、当前 Definition GET/PUT、父行锁与 `base_version_id` 过期保存 409、资源/工具预检、整批原子导入、必需秘密运行阻断、多 Agent ref 消歧及新建 Agent 的 Prompt 归属复制均已落地，再新增契约测试 13 项。未增加数据库迁移，未修改依赖 pin；这些契约构成后续最小 Studio 的后端基础。

**2026-11-10 最小 Studio 实现状态：** `frontend/src/pages/AgentStudio.tsx` 已接通单 Agent canonical Definition JSON 编辑、显式资源映射、一次性秘密输入、乐观并发保存、版本 A/B 选择、相同输入运行、输出/指标比较及 Trace 切换。过期保存显示 409 且保留草稿，重载后才采用服务器最新版本；秘密值保存后清空，不进入 localStorage。隔离 Mock 浏览器覆盖 v2 保存、v1/v2 不同输出与各自 Trace、外部 v3 导致的冲突及重载后 v3 调试。多 Agent、创建向导、自然语言配置、自动 Prompt 修复及真实 Provider 质量验收不在此切片。

# AgentForge P0 开发基线与能力事实矩阵

> 建立日期：2026-11-10。基线 commit：`88aa1ac17d3794431887f206404e11e27db7b08d`。
>
> 本文对应[开发路线图](../../开发路线图.md) 16.1–16.6 的实测、16.7 设计、16.8 最小 Studio 及 16.9 旗舰包设计状态，只记录已经执行的检查或已闭合的文档决策。`.scratch/roadmap-baseline-2026-11-10/`、`.scratch/studio-baseline-2026-11-10/` 与 `.scratch/research-assistant-2026-11-10/` 是本机证据目录，不进入 Git；其他机器应重新执行命令，不能把本机结果当作发布证明。

## 1. 当前判定

**16.1–16.6 已完成，P0 基线关闭。** 原 `backend/requirements.txt` 未升级，隔离 Python 3.12.10 环境完成安装、`pip check`、editable 安装、应用导入和完整后端验证，最终为 **350 passed、0 failed、退出码 0**。最终 lockfile 的前端干净安装、lint、typecheck 和 build 均通过；隔离浏览器的创建、版本、运行终态、Trace、评测、门禁徽标及版本锁定交付全部通过；本地 CPU Provider 的普通、流式、工具调用和超时分类通过。

16.7 的 Agent Schema v1 设计评审结论已闭合。2026-11-10 完成了 16.8 所需全部后端前置契约：统一公开投影、秘密状态机、Prompt/知识快照、指定版本运行、envelope 2、当前 Definition 写入、`base_version_id` 乐观并发、canonical 资源映射和必需秘密阻断均有实现及测试。随后完成的**最小单 Agent Studio** 已贯通 Definition 编辑、版本保存、指定版本调试、同输入 A/B 比较和 Trace，并有独立数据库的浏览器证据；这不表示路线图第 4 步的创建向导、自然语言草案、完整导航重组等增强均已完成。前端审计仍有 10 个 moderate 和 1 个 high，GPU Provider 仍失败，均作为独立后续债务保留。

16.9 的**本地研究助理旗舰包设计与资料准备**已闭合：4 份 MIT 公开资料、canonical 候选包、10 条用例、引用/拒答契约和独立验证计划均有静态校验。5 条真实 Ollama 直接客户端探针均执行成功，但按完整契约为 0/5，通过质量验收前不得称为旗舰成品；该探针不经过 AgentLoop、Studio、门禁或交付，不能替代后续端到端质量证据。

当前工作树在本轮开始前已有 README、文档整理和诊断文件改动。本轮没有回退、暂存或覆盖这些修改，也没有清理原有运行数据。

## 2. 环境与可复现边界

| 项目 | 本机事实 | 与项目声明的关系 |
| --- | --- | --- |
| OS | Windows 11，64 位 | 中文 Windows 的 `cmd` 错误文本不是英文 |
| 全局 Python（对照组） | 3.14.6 | 此前 349 passed 只代表全局混合依赖，不替代原 pin 验收 |
| 隔离 pinned Python | 3.12.10，独立运行时与 venv | 满足项目 `>=3.11`；安装器 SHA-256 和 freeze 已留档 |
| Node / npm | 24.21.0 / 11.19.0 | 前端使用 Vite 5、React 18、TypeScript 5 |
| pytest / pytest-asyncio | 全局 9.1.1 / 1.4.0；pinned 8.3.4 / 0.25.0 | 两组结果分开记录，不混用计数 |
| SQLAlchemy / FastAPI / Pydantic | 全局 2.0.51 / 0.141.1 / 2.13.4；pinned 2.0.36 / 0.115.6 / 2.10.3 | pinned 与 `backend/requirements.txt` 一致 |
| 本地 Provider | Ollama，仅访问 `127.0.0.1:11434` | 未读取或记录 API 密钥，未使用云模型 |

本轮未修改全局 Python 包。Python 3.14 干净 venv 安装原 pin 时，`pydantic-core`、`aiohttp`、`tiktoken` 需要源码构建，而本机缺少 MSVC `link.exe`，该失败原样保留。随后使用官方 Python 3.12.10 隔离运行时安装**相同 requirements pin** 与 editable 项目，`pip check` 通过；最终完整套件 350/350。3.14 的安装边界仍真实存在，但不再阻塞项目声明支持的 3.12 环境验收。

根 `pyproject.toml` 与 `backend/pytest.ini` 都是现有 pytest 入口：无参数全量运行使用前者，指定 `backend/tests/...` 时使用后者。两处已同步 `asyncio_mode = auto` 和 function fixture loop scope；最终干净日志不再出现 loop-scope 未设置警告。

## 3. 标准复现命令

在仓库根目录执行；`<PinnedPython>` 指隔离 Python 3.12.10 venv 的解释器绝对路径，不能替换为版本不明的全局 `python`：

```powershell
<PinnedPython> -m pip install -r backend/requirements.txt
<PinnedPython> -m pip install -e .
<PinnedPython> -m pip check
<PinnedPython> examples/local-research-assistant/validate_pack.py
<PinnedPython> -m pytest -q
<PinnedPython> -m pytest backend/tests/test_direction1.py backend/tests/test_direction2.py backend/tests/test_direction3.py backend/tests/test_direction4.py backend/tests/test_direction5.py -q
npm ci --prefix frontend
npm run lint --prefix frontend
npm run typecheck --prefix frontend
npm run build --prefix frontend
git diff --check
```

本机 pinned 解释器位于 `.scratch/runtime/venv-py312/Scripts/python.exe`，freeze 为 `python312-freeze.txt`。Provider 验收脚本位于 `.scratch/roadmap-baseline-2026-11-10/verify_local_provider.py`，通过 `AGENTFORGE_LOCAL_MODEL` 指定模型；它直接调用项目的 `make_client`，不伪造 HTTP 响应，也不连接非回环地址。

## 4. 能力事实矩阵

| 能力 | 当前事实 | 已有证据 | 未证明 / 后续边界 |
| --- | --- | --- | --- |
| Agent 创建、编辑、列表、运行 | 已有 ORM、API 和表单式页面 | 后端测试；本轮隔离 Mock 浏览器全链路通过 | 不是统一 Schema 驱动的创建工作室；真实模型应用质量另验 |
| Agent 版本与配置快照 | 已有不可变版本和版本 diff | `test_direction1.py` | 不等于自动 Prompt 修复或模型训练 |
| 模型 Provider | 有 Mock、OpenAI 兼容和 Anthropic 客户端 | CPU Ollama 客户端验收通过 | GPU 模型失败；完整 Agent API 的本地真实运行待验 |
| 知识库与引用 | 有知识库、文档、检索和 Prompt 上下文实现；16.9 已有公开候选资料包与引用/拒答契约 | 既有后端测试；候选包静态校验及 2 项回归；5 条真实直接客户端探针原始输出 | 探针完整契约 0/5；尚无全文核验、独立检索 Trace、真实 embedding 验收和旗舰系统验收 |
| 记忆 | 有短期上下文、持久记忆、TTL 与检索实现 | 记忆相关后端测试 | 未证明长期质量收益或所有向量后端可用 |
| 工具与执行 | 有注册、风险、审批、本地/容器执行和 Trace | 工具、沙箱和恢复测试 | 危险执行不是无条件安全；复杂协议和自主多 Agent 不在本轮 |
| 暂停、恢复与持久状态 | 有 RunState、暂停和恢复 | 定向恢复测试通过 | 完整套件与隔离浏览器仍必须复验 |
| 评测、门禁与回归 | 有 EvalSuite、GateRun、版本比较和 bypass 审计 | 方向测试及完整 pytest | Mock 分数只证明工程机制，不证明真实模型质量 |
| 交付、团队与反馈 | 有部署、权限、审计、反馈等实现 | 既有后端测试 | 不等于生产、公网或大规模协作已验收 |
| CLI / SDK / Git 工作流 | 有 Python CLI；部分导入导出能力 | 既有测试和 CLI 代码 | 稳定 Agent Package、SDK 契约和 Git PR 工作流未完成 |
| Agent Schema / Studio | canonical Definition、版本 API 与单 Agent 三栏 Studio 已接通 | 后端契约测试；Studio 隔离浏览器 v1/v2/v3 流程 | 创建向导、自然语言草案、多 Agent 和真实 Provider Studio 质量另验 |
| 本地研究助手 | 尚无完整旗舰 Agent 包 | 无 | 是下一阶段主场景，不能借 PyAgent 的结果宣称为 AgentForge 能力 |

## 5. 已执行失败分类与处理

### 5.1 后端

- **初始完整复现：**345 passed、2 failed。两项都是 Windows 沙箱测试要求英文 `not recognized/not found`，而本机返回中文且受 OEM 解码影响。修复是验证非零退出、结构化 stderr 和可执行文件名，没有改产品行为来迎合英文文本。
- **测试隔离与并发：**全局内存 SQLite 原先不在用例后清表，且 `StaticPool` 让并发 AsyncSession 实际共享一个连接，出现 `agent_runs` 更新竞争。pytest 现使用独立临时文件数据库、每连接 WAL/busy timeout 和完整清表；普通测试关闭 lifespan 自动迁移，Alembic 专项仍执行真实迁移。命名 scorer 与 WS 修复后的最终完整套件为 349/349。
- **暂停/恢复：**完整复跑出现 checkpoint `duration_ms=12`、返回值 `duration_ms=13`。根因是两个时点分别取计时值。现已在最终提交前同步 checkpoint、`Run` 与 `LoopResult` 的耗时；恢复及记忆暂停定向测试 6/6 通过。
- **命名 scorer 门禁：**普通评测接受 `exact_match`，门禁却要求 `EvalScorer` 实例。新增 `MetricScorer` 双接口适配器，保留旧函数调用并支持结构化评分；新增门禁用例，相关 45 项定向测试通过。
- **WebSocket 终态竞态：**snapshot 可能读到 `running` 和完整 Trace，而 complete 在随后订阅前丢失。现改为先订阅再读 snapshot，排队事件在 snapshot 后发送，并补充确定性的间隙注入回归；WS、lifespan、权限和门禁相关 29 项定向测试通过。

### 5.2 前端

- 初始 `npm run lint` 失败：脚本存在，但 ESLint 未安装；类型检查与生产构建分别通过。
- 已加入 ESLint flat config、明确开发依赖和 `package-lock.json`。
- 首次被前台超时中断的安装留下不完整依赖，随后出现 `concat-map` 缺失；lockfile 也曾与 package 不同步。已修复 lockfile，并改用保留退出码的后台 `npm ci` 重建，不能将中间状态记为 lint 通过。最终 lockfile 再次 `npm ci` 成功，ESLint 9.39.5 的 lint、typecheck 和 build 均通过。
- 非强制 `npm audit fix` 消除了两个 low 项；最终仍有 **10 moderate、1 high**。Vite 高危项与 React Router 修复需要大版本升级，当前 `typescript-eslint` 审计项无可用修复；未使用 `--force`。审计原文见 `frontend-audit-after.json`。
- 浏览器还发现运行终态和门禁徽标不一致。前端在 `run.complete` 后按 run ID 重新读取 canonical RunDetail，避免 step/complete 同批到达时 Trace 状态不完整；门禁轮询结束后重新加载版本，确保版本徽标与历史门禁一致。

### 5.3 本地 Provider

- `qwen2.5:3b`：普通、流式和工具调用均因 Ollama CUDA `device kernel image is invalid` / runner 崩溃失败。证据：`local-provider.log` 与 `local-provider-result-gpu.json`。
- `qwen2.5:3b-cpu`：普通响应、流式终止块、解析后的 `get_document_title(document_id="doc-123")`、token/模型元数据以及 `LLMTimeoutError` 全部通过。证据：`local-provider-cpu.log` 与 `local-provider-result-cpu.json`。
- 结论只覆盖 Provider 客户端，不覆盖研究助手质量、完整 API、UI 或持续稳定性。

### 5.4 隔离浏览器

- 每次运行使用新的工作目录、SQLite 数据库和 Vite Node 进程，端口 8000/5174 在启动前检查，结束后只终止本轮进程；最终检查没有遗留监听者。每次失败日志、数据库、截图和 WebSocket 帧分别保留，没有覆盖成成功结果。
- 早期尝试分别暴露了本地化状态断言、旧 scorer 名称、命名 scorer 门禁错误和 WS 竞态；均按实际原因处理。最终日志为 `browser-regression-final-reconciled.log`，成功证据目录为 `browser/run-33bdfd63978c4d7a85c3c0ce9262ec19/`。
- 最终断言覆盖 UI 创建、API 编辑生成版本 2、UI 终态、canonical RunDetail 与 final-answer Trace、1/1 评测、UI 门禁、API 与页面版本徽标均为 passed、版本锁定交付及 trace ID。`errors` 为空；Mock 仅用于确定性工程验证。

### 5.5 干净 Python 环境与原 pin 验收

- `python-venv-install.log` 保留 Python 3.14.6 的完整安装失败：原 pin 原生依赖转入源码构建，本机没有 MSVC linker。该问题没有被隐瞒，也没有通过升级 requirements 绕过。
- 使用官方 Python 3.12.10 隔离运行时后，相同 pin 安装成功；`python312-pin-install.log`、`python312-editable-install.log`、`python312-freeze.txt` 和 `python312-installer-sha256.txt` 分别保存安装、editable、实际版本与安装器哈希，`pip check` 退出码 0。
- pinned FastAPI 0.115.6 首次导入对 204 路由断言失败。五个路由声明已显式 `response_model=None`，工具元数据契约回归 12/12 通过；独立应用路由表检查发现当前 14 个 204 路由均无 response model。
- 移除自定义 session `event_loop` 后，原先 140 个顺序失败的最小组合回归为 42/42；两个 pytest 配置入口均显式使用 function fixture loop scope。
- 随后完整套件暴露 1 个 Memory TTL 共享内存库生命周期失败。`db_session` 改为每测试独立文件 SQLite，Memory TTL 文件 4/4；没有删除测试或修改产品断言。
- 最终公开 OpenAPI 契约版本 `python312-backend-full-verified.log` 为 **350 passed、0 failed、1313 warnings、退出码 0**。较旧全局基线多的一项是 204 路由契约回归；较早的 `python312-backend-full-clean.log` 也保留为中间通过证据。
- 新增契约最初依赖 `app.routes` 内部结构，在全局 FastAPI 0.141.1 下遇到 lazy `_IncludedRouter` 并失败；改为断言公开 OpenAPI 的 204 response 不含 `content` 后，两套环境定向均 12/12。最终全局 `python314-backend-full-clean.log` 也为 **350 passed、0 failed、退出码 0**。
- 安装、204、异步顺序、SQLite 生命周期和跨版本路由内省失败的复现、根因、无效尝试及中文复盘见[故障案例库](../troubleshooting/p0-pinned-python/README.md)。

### 5.6 Agent Schema 首批后端前置契约

- 新增 `backend/app/agent/schema.py` 与 `backend/tests/test_agent_schema_contracts.py`，并加入 legacy/security JSON fixtures。公开 DTO 统一递归处理敏感键、密文及已知秘密值；版本 Definition 不返回内部密文，秘密以 JSON Pointer `secret_refs` 表达。
- 掩码 overwrite 可继承原秘密，create 不生成伪密钥；`null`/空字符串清除。数组缩短、身份重排和父结构冲突返回 `secret_path_conflict`，测试确认没有追加版本。legacy envelope 1 导入支持 `dry_run=true`，分别报告 `missing_secrets` 与 `inherited_secrets`，dry-run 不写 Agent 或版本。
- 新 AgentVersion 写入 `schema_version: 1`，固定 Prompt 原始 UTF-8 内容及 SHA-256、排序后的知识绑定。Prompt/知识绑定变化追加版本；旧版本运行、交付和资源缺失路径不使用当前绑定静默兜底。无 Schema 标记的 legacy snapshot 保持原重建分支。
- `RunCreate` 接受可选 `agent_version_id`；服务端校验归属并从不可变快照重建执行对象，响应返回实际版本 ID。13 项新增契约测试在 pinned 环境单独通过。
- 最终 `agent-schema-backend-py312-final.log` / `.exit` 为 **363 passed、0 failed、1389 warnings、退出码 0**（Python 3.12.10，215.38 秒）；`agent-schema-backend-py314-final.log` / `.exit` 为 **363 passed、0 failed、1393 warnings、退出码 0**（Python 3.14.6，152.60 秒）。原 `backend/requirements.txt` 未改变，`pip check`、关键 Ruff、新增模块完整 Ruff、`compileall` 和 `git diff --check` 均通过。
- 首批当时尚未实现 envelope 2、当前 Definition GET/PUT、`base_version_id` 和 canonical 资源映射；这些范围由下一批闭合，Studio 仍不在两批后端工作中。

### 5.7 Agent Schema 第二批后端前置契约

- 默认导出改为 envelope 2 + Definition v1，JSON/YAML 共用严格 Pydantic 校验；`?envelope=1` 仅作为显式有损导出并返回警告头。新增 `GET/PUT /agents/{id}/definition`，保存先锁 Agent 父行并核对 `base_version_id`，过期写入返回 409 和最新版本信息。
- canonical 导入通过 multipart `resource_map` / `secret_inputs` 接收显式资源映射和一次性秘密，预检返回 `resolved/unresolved/unsupported_tools/missing_secrets/inherited_secrets`。未解析资源、禁用/未知工具、重复名称或秘密结构冲突均整批拒绝；写入阶段第二条失败的事务回滚测试也确认没有首个 Agent/版本残留。
- 新建 Agent 的 Prompt 映射在同团队复制为当前 Agent 自有模板；overwrite 不允许借用其他 Agent 模板。知识库 ref 跨版本稳定并按 ref 序列化；多 Agent 包重复 ref 使用 Agent 命名空间消歧，同名 Prompt 支持限定映射键。
- required 秘密缺失允许保存定义，但 API Run 与执行循环均在运行前阻断；普通当前 Agent 执行读取最新版本元数据，指定版本执行只读取自身快照。无需新增 ORM 列或 Alembic 迁移，`extensions`、秘密元数据和稳定 ref 均保存在现有快照中。
- 新增 `test_agent_definition_contracts.py` 13 项，覆盖 Unicode/转义指针、JSON/YAML、秘密继承与阻断、409、显式映射、跨团队拒绝、Prompt 复制、整批回滚及多 Agent 独立映射。两套后端计数及当时的前端与浏览器证据见第 6 节；随后最小 Studio 见 5.8。

### 5.8 最小单 Agent Studio（16.8）

- 新增 `frontend/src/pages/AgentStudio.tsx` 及 API 类型、双语资源和响应式三栏样式。左栏编辑公开 canonical Definition，必要时提供 `resource_map` 和一次性 `secret_inputs`；中栏选择版本 A/B 并复用完全相同的输入；右栏复用 canonical `TraceTree` 切换两侧步骤。
- 保存调用现有 `PUT /agents/{id}/definition`，不在前端生成版本号或密文。成功后采用响应中的新 `base_version_id`、刷新版本列表并清空秘密输入；409 `stale_base_version` 保留本地草稿，只有用户显式“重载最新版本”才替换草稿。
- A/B 两个请求都提交不可变 `agent_version_id`，通过 RunDetail 轮询终态并核对输出、耗时、步数和 token。比较只证明所选版本在所记录输入与环境中的工程行为，不把差异自动归因于 Prompt，也不证明真实模型质量。
- 隔离 Studio 浏览器实际完成：UI 保存 v2、秘密输入清空及导出无明文/密文、v2/v1 同输入得到不同输出和 Trace、外部写入 v3 后 UI 409 草稿保留、重载 v3 后指定版本单跑成功。唯一 409 按请求方法和 URL 单独登记，其他 HTTP/console/page error 均为空。
- 既有核心浏览器脚本改为按 tab 名称而不是序号定位，新增 Studio 后重新通过创建、运行、评测、门禁和版本锁定交付。两组浏览器均使用独立 SQLite，结束后 8000/5174 无监听者。

### 5.9 本地研究助理旗舰包设计（16.9）

- 新增[专项设计](LOCAL_RESEARCH_ASSISTANT.md)和 `examples/local-research-assistant/`。4 份项目原创 MIT Markdown 共 4103 个 Python Unicode 字符，manifest 固定逐文件字节数及 SHA-256；envelope 2 + Definition v1 使用 `ollama` / `qwen2.5:3b-cpu` / 回环端点，无秘密、无工具，知识 ref 必须显式映射。
- 10 条 JSONL 候选问题可由现有统一 `parse_dataset` 读取，含 4 条直接、2 条跨资料、3 条证据不足和 1 条提示注入。`required_sources`、引用和拒答字段进入 metadata；当前通用文本相似度 scorer 不会自动执行这些语义规则，四层持久化模型留给 16.10。
- `validate_pack.py` 无网络校验 canonical Schema、公开安全、回环端点、许可、文件集合、哈希、资料规模、用例唯一性和来源章节。新增 2 项测试，其中一项复制包后篡改资料并确认 SHA-256 mismatch 被阻断；定向结果 2 passed，Ruff/compileall 通过。
- 真实探针使用生产切片和默认 hashing encode 的内存 top 5，直接调用本地 Ollama，不用数据库/vector store、AgentLoop、Studio、GateRun 或 Deployment。模型 digest 为 `b3ba2a0c733187017162d07b79a210d9c163a6a260c1fba5166b72b11eabd981`，5/5 请求完成、模型 ID 匹配、无 Provider 错误，耗时 1176–7507 ms。
- 机械检查为 false；逐条按公开契约复核为 **0/5 完整通过**。正常题缺来源列表、跨资料/版本题回答不完整，GPU 拒答未指出具体缺口，注入题拒答后仍附 5 个弱相关引用。失败作为 Definition A 保留，后续 Prompt B 必须同输入、同模型 digest、同资料及检索条件比较。完整探针由后台进程完成，标准输出给出执行/机械结果；退出码未捕获，不写成 0。首次前台 30 秒超时产生的两题部分结果另存，不计入最终 5 题。
- 默认 hashing 不是 bge-m3；本机存在 Ollama embedding 模型不等于已接入。当前 `search_knowledge` 是 Memory 工具，候选包不使用它伪装 KnowledgeBase 检索；独立检索 Trace、证据分数门槛、真实 embedding、受控只读工具、完整多轮质量、交付和反馈均未验收。

### 5.10 四层质量数据模型设计（16.10）

- 新增[四层质量数据模型 v1](QUALITY_DATA_MODEL.md)：Run 以可空增量字段保存 answer/tool/retrieval/execution 原始观察；GateRun 以自包含 conditions 固定 case/scorer/模型/检索/运行次数，并按 case/attempt 引用实际 Run。旧 `details/diff` 保留，新增层变化、证据引用和 comparability；legacy baseline 不允许产生四层 fixed/new_failure 结论。
- Feedback 直接固定 `Run.agent_version_id`，新增追加式人工 FeedbackResolution 关联候选 GateRun 的 case diff；反馈转用例或 Gate 自动通过均不等于 confirmed_fixed。后续 0012 仅增加 nullable JSON-Text/Integer/外键及 resolution 表，不按时间猜版本、不批量回填质量对象。
- 4 份明确虚构的 Run/Gate/Feedback/legacy 示例通过静态解析、JSON Pointer、来源注入、层状态、Run/Version/Gate/diff/Feedback 交叉链接等 **76 项检查**。独立旧 Reader 探针确认当前 RunOut 未配置 extra，Pydantic 默认 ignore，新增 quality 字段不进入旧模型。
- 本轮只有设计、示例和文档，没有修改产品代码或依赖，没有实施 0012、AgentLoop/检索采集、API/TS/UI，也没有执行新的迁移、E2E、浏览器或真实 Provider 质量验收；既有 378 项 pinned 后端结果继续作为最近代码全量基线。

## 6. 证据索引与最终回归

| 检查 | 命令 / 证据 | 结果 |
| --- | --- | --- |
| 初始完整后端 | `backend-full-before.log` | 345 passed，2 failed |
| 第一轮修复后完整后端 | `backend-full-after-test-fixes.log` | 346 passed，1 failed（暂停耗时） |
| Windows 与方向 1 定向 | `backend-targeted-after.log` | 16 passed |
| 恢复与暂停定向 | `backend-loop-targeted-after.log` | 6 passed |
| 命名 scorer 与普通评测定向 | `backend-file-db-targeted.log` | 45 passed |
| WS、lifespan、权限与门禁定向 | `backend-ws-race-targeted.log` | 29 passed |
| 全局环境完整后端（修复前对照） | `backend-full-final.log` | **349 passed，0 failed**；退出码 0；不是 pinned 验收 |
| pinned 204 定向回归 | `python312-backend-full.log` → 修复后工具元数据定向 | 首次导入失败；修复后 12 passed，新增 1 项路由契约 |
| pinned 异步顺序回归 | `python312-backend-full-after-204.log`、`python312-loop-targeted.log` | 由 140 failed / 210 passed 收敛为 **42 passed** |
| pinned Memory TTL 中间失败 | `python312-backend-full-after-asyncio.log` | 349 passed / 1 failed；共享内存 SQLite schema 生命周期 |
| pinned 最终完整后端 | `python312-backend-full-verified.log` / `.exit` | **350 passed，0 failed，退出码 0**；182.98 秒；公开 OpenAPI 契约版本 |
| 全局环境修复后完整后端 | `python314-backend-full-clean.log` / `.exit` | **350 passed，0 failed，退出码 0**；130.10 秒；公开 OpenAPI 契约版本 |
| Schema 首批契约 pinned 完整后端 | `agent-schema-backend-py312-final.log` / `.exit` | **363 passed，0 failed，退出码 0**；215.38 秒；含 13 项新契约测试 |
| Schema 首批契约全局完整后端 | `agent-schema-backend-py314-final.log` / `.exit` | **363 passed，0 failed，退出码 0**；152.60 秒；与 pinned 结果分开记录 |
| Schema 第二批契约 pinned 最终后端 | `agent-schema-final2-py312.out.log` / `.exit` | **376 passed、0 failed、1553 warnings、退出码 0**；259.23 秒；含第二批 13 项契约测试 |
| Schema 第二批契约全局最终后端 | `agent-schema-final2-py314.out.log` / `.exit` | **376 passed、0 failed、1557 warnings、退出码 0**；168.09 秒；与 pinned 结果分开记录 |
| Schema 第二批前端质量 | `agent-schema-frontend-{lint,typecheck,build}.log` / `.exit` | lint、typecheck、build 均退出码 0；未修改前端依赖 |
| Schema 第二批最终隔离浏览器 | `agent-schema-browser-final.out.log` / `.exit` | **passed=true，errors=[]，退出码 0**；核心链路通过，8000/5174 均已清理；证据 `browser/run-257542bda69b44a6b7ba1b19e282bfef/` |
| Studio pinned 完整后端 | `.scratch/studio-baseline-2026-11-10/py312-full.out.log` / `.exit` | **376 passed、0 failed、1552 warnings、退出码 0**；280.56 秒；Python 3.12.10 |
| Studio 全局对照完整后端 | 同目录 `py314-full.out.log` / `.exit` | **376 passed、0 failed、1557 warnings、退出码 0**；260.84 秒；Python 3.14.6，与 pinned 结果分开记录 |
| Studio 前端质量 | 同目录 `frontend-{lint,typecheck,build}.log` / `.exit` | 三项均退出码 0；未修改前端依赖或 lockfile |
| Studio 隔离浏览器 | 同目录 `studio_browser.py`；`browser/run-33ffc9bddfbf432a94958fbbd08042c9/result.json` | **passed=true，errors=[]**；四项 Studio 断言、两张截图；唯一预期 409 单独记录；退出码 0 |
| Studio 加入后的核心浏览器 | 同目录 `core-browser.out.log` / `.exit`；`roadmap-baseline-2026-11-10/browser/run-b5dea705f57b435cbf2970b4b7824a3e/` | **passed=true，errors=[]，退出码 0**；创建、运行、评测、门禁和交付六项断言；端口均清理 |
| 16.9 候选包静态校验 | `.scratch/research-assistant-2026-11-10/static-validation.log` / `.exit` | 4 份资料、4103 字符、10 条用例；`scope=static-package-only`，网络请求 0，真实质量未评价 |
| 16.9 定向回归与 Ruff | 同目录 `pack-targeted.out.log` / `.exit`、`ruff.out.log` / `.exit` | **2 passed，退出码 0**；Ruff 全过；篡改资料哈希会被拒绝 |
| 16.9 pinned 完整后端 | 同目录 `py312-full.out.log` / `.exit` | **378 passed、0 failed、1553 warnings、退出码 0**；204.28 秒；Python 3.12.10，包含 2 项新包回归 |
| 16.9 全局 Python 定向对照 | 同目录 `py314-pack-targeted.out.log` / `.exit` | Python 3.14.6，**2 passed、退出码 0**；只是定向对照，不冒充第二套全量 |
| 16.9 真实 Provider 可行性探针 | 同目录 `provider-probe.json`、`manual-review.json`、`provider-probe.out.log` | 5/5 请求完成、无 Provider 错误；`mechanical_checks_passed=false`，完整契约 **0/5，质量不接受**；不是 AgentLoop/Studio/门禁/交付证据，进程退出码未捕获 |
| 16.9 文档与 JSON | 同目录 `validate_docs.py`、`docs-validation.log` / `.exit` | 10 份文档的 **69 个本地目标**及 6 组 JSON/JSONL 均通过，errors=[]，退出码 0 |
| 16.10 设计静态交叉校验 | `.scratch/quality-model-2026-11-10/validate_design.py`、`validation.json`、`validation.log` / `.exit` | 4 份虚构契约示例，**76 项检查通过，errors=[]**；覆盖来源注入、JSON Pointer、四层状态及 Run/Gate/diff/Feedback 链接 |
| 16.10 旧 Reader 探针与定向回归 | 同目录 `legacy-reader-probe.json` / `.exit`、`reader-tests.log` / `.exit` | 探针 `passed=true`：当前 RunOut 的 extra 未显式配置，实际 unknown-field 策略为 ignore，新增质量字段不进入旧模型；`test_schemas.py` **10 passed、退出码 0** |
| 16.10 文档与 JSON | `.scratch/research-assistant-2026-11-10/validate_docs.py`；质量目录 `docs-validation-final.log` / `.exit` | **12 份文档、82 个本地目标、10 组 JSON/JSONL**，errors=[]，退出码 0；不是产品 E2E/浏览器证据 |
| GPU Provider 独立分诊 | 同目录 `gpu-triage.json`；既有 `local-provider-result-gpu.json` 与脱敏服务日志模式 | 仍为失败；定位到 CUDA v12 `device kernel image is invalid` / `0xc0000409`。CPU alias 不算 GPU；仅核对既有证据和配置，本轮未新增 GPU 推理 |
| 前端干净安装 | `frontend-npm-ci-final-lock.log` | 成功；退出码 0 |
| 前端 lint / typecheck / build | `frontend-lint-final.log`、`frontend-typecheck-final.log`、`frontend-build-final.log` | 三项均退出码 0 |
| 最终隔离浏览器 | `browser-regression-final-reconciled.log` | **passed=true**，errors 为空，六个核心断言通过 |
| CPU Provider | `local-provider-cpu.log` | **passed=true**，普通/流式/工具/超时通过 |
| 原 pin 干净 Python 环境 | `python312-pin-install.log`、`python312-freeze.txt`、`python312-editable-install.log` | **完成**；Python 3.12.10 安装相同 pin，`pip check` 通过。3.14 构建失败另存于 `python-venv-install.log` |
| 前端依赖审计 | `frontend-audit-after.json` | 10 moderate、1 high；保留为后续债务 |

## 7. 下一动作

16.1–16.10 的任务卡范围已有上述证据，其中 16.9 只完成旗舰包设计准备，16.10 只完成[四层质量数据模型与迁移设计](QUALITY_DATA_MODEL.md)。下一动作是按第 9.1 节顺序实施纯契约 fixtures、additive 0012、AgentLoop/KnowledgeBase 采集、EvalRunner Run ID 传播、Gate conditions/evidence/diff 和反馈 resolution，再执行真实旧库迁移、Mock E2E 与隔离浏览器验收。随后才能按专项计划完成真实 embedding、受控只读工具、同条件 Prompt B、多轮真实质量、人审和交付。16.9 的 0/5 探针结果不得写成质量通过，也不表示模型普遍不可用；direct probe 不得回填成四层 Run。16.11 视频仍等待旗舰实现与质量接受，GPU Provider 和前端依赖审计继续作为独立任务跟踪；不直接跳到多 Agent、自动修复 Prompt 或完整四工作空间导航。

## 8. 2026-11-12 四层质量闭环交付补记

本轮以既有未提交工作树为起点，没有回退、暂存或覆盖此前 Studio/Schema/研究包修改，也没有创建 Git commit。完成范围限定为四层质量工程闭环及演示，不把第 9 步研究助理或生产验收顺带标为完成。

### 实施事实

- 0012 `b72f41a9c012`：Run 五个可空质量字段、Gate 三个字段、Feedback 精确版本外键和追加式 `feedback_resolutions`；0011 原文、旧摘要和旧字段插入保持兼容。
- AgentLoop/Step/KnowledgeBase 产生实际 Run 观察；EvalRunner 传播确切 `run_id`；Gate 冻结条件、保存逐案断言并生成层级 diff；重复 Provider call ID 按顺序唯一映射。
- Feedback 复用 Gate 原 EvalCase，人工 `confirmed_fixed` 要求原失败 Run 在 comparable diff 基线侧及当前 attempt 全过；自动 Gate 不写人工结论。
- React Run/Gate 页面展示原始观察与判定，支持确切 Run 深链接；运营页支持转换与人工复核，并修复“已有 expected_answer 但未二次编辑时无法转换”的状态回退问题。

### 本轮实测

| 检查 | 结果 |
| --- | --- |
| pinned Python 3.12 全量后端 | **438 passed、0 failed**，218.58 秒；日志 `.scratch/completion-2026-11-12/backend-final.*` |
| Alembic 与质量契约 | **60 passed**；新增旧库保留后 Alembic 全文件 **6 passed**，含 0011→0012、旧字段插入和 round-trip |
| 四层/反馈/五方向/API 定向 | **19 passed**；重复 call ID 修复相关回归 **12 passed** |
| 前端 | ESLint、TypeScript、Vite production build 通过 |
| 静态检查 | secret 扫描无发现、Python compileall、涉及文件定向 Ruff、`git diff --check` 通过；全仓库历史 Ruff 风格债务未用批量改写掩盖 |
| 连续 Edge 闭环 | `.scratch/completion-2026-11-12/browser/run-f8f2576103214c60a31ba319329d83b6/`；passed=true，errors=[] |
| 分段 Edge 录制 | `.scratch/completion-2026-11-12/browser-segments/run-f833e59f3192437786a1595808a6b739/`；6 个独立 WebM，passed=true，errors=[] |
| 最终媒体 | `AgentForge-demo.mp4/.srt/.gif` 与 `docs/assets/agentforge-quality-loop/media-validation.json`；公开展示版 **183.328 秒、25 条字幕**；逐段录屏、中文 TTS、完整解码、音轨、字幕、抽帧和灰白过渡帧检测通过 |

连续和分段浏览器均执行 v1 通过、v2 new_failure、v3 fixed、确切 Run、反馈复用原用例、人工 confirmed_fixed、固定版本 REST/独立页交付及移动宽度检查。最终视频只采用分段素材，避免连续录屏场景交界帧错配。制作脚本为 `scripts/tests/quality_browser_segments.py` 与 `scripts/demo/produce_quality_video.py`。

**展示版修订：**根目录视频、字幕和 GIF 已改为正常产品展示叙事，依次介绍平台能力、固定回归、执行细节、四层定位、反馈复核和版本交付；不再把工程限制作为公开旁白主线。GIF 改取 `gate-fixed` 场景。旧工程版 MP4/SRT/GIF、旁白和媒体报告完整保存在 `.scratch/completion-2026-11-12/engineering-demo-archive/`，未删除历史证据。详细后续工作见[后续开发规划](FUTURE.md)。

### 未扩张的边界

当前每案一次执行，未交付多轮波动统计；检索记录证明授权查询与注入事实，不等于语义相关性或引用支持度通过。真实研究助理仍是此前 0/5 完整契约的失败基线，本轮未把它改写为旗舰成品。真实 Provider 应用质量、多 Agent、生产 Docker/Redis/PostgreSQL、公网安全、iframe 全矩阵、性能、首次体验研究和目标信创环境均需独立任务与证据。

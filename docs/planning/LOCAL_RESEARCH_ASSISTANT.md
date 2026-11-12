# 本地研究助理旗舰包设计（16.9）

> 日期：2026-11-10。状态：**16.9 设计与资料准备；不是路线图第 9 步完整旗舰验收报告。**
>
> 本设计不沿用客服/退款主线，也不把 Mock 浏览器结果、Provider 协议成功或单次模型回答写成研究质量证明。可校验包位于 `examples/local-research-assistant/`，实测结果与证据索引回填开发基线。

## 1. 目标用户、场景与边界

目标用户是需要在私有或本地资料上做小规模事实核查的 Agent 创建者和维护者。首个场景是：导入一组明确公开的 AgentForge 资料，提出事实、跨资料和限制类问题，检查回答是否由实际证据支持，并在资料不覆盖时拒答。

“旗舰”指未来端到端证据最完整，不指通用研究能力。当前 16.9 只闭合设计、资料、候选定义、问题、引用规则、证据不足用例和限制；受控工具、真实 embedding 接入、检索质量门槛、四层质量模型、完整 Studio 对比、门禁、交付和反馈闭环仍需后续实测。

## 2. 设计决策

1. **自包含公开资料**：使用 4 份项目原创、MIT 许可的 Markdown，不抓取外部网页，不包含个人数据、密钥或私人文档。`sources.json` 固定字节数和 SHA-256。
2. **复用 canonical Schema**：候选包为 envelope 2 + Definition v1，知识库只写逻辑 ref，导入时必须显式 `resource_map`，不按名称猜资源。
3. **真实 Provider 候选而非默认承诺**：`ollama` + `qwen2.5:3b-cpu` + 回环端点是本机候选；模型别名不是跨机器统一版本。云模型只能显式替换并另行记录，不能自动 fallback。
4. **v0 不启用工具**：知识由运行器自动检索注入。当前 `search_knowledge` 查询 Memory，不是 KnowledgeBase 文档工具，不能用于伪装文档工具 Trace。
5. **拒答与引用都是可检查行为**：不仅看文本相似度；必须核对来源定位、相邻结论的支持关系、弱相关候选和拒答是否恰当。
6. **保留小样本失败**：10 条候选用例用于暴露问题，不通过删除难题、调低预期或只保留最佳一次来制造成功率。

## 3. 包结构与资源语义

| 资源 | 本包决定 | 验收关注点 |
| --- | --- | --- |
| Prompt | Definition 内原始 `system_prompt`，未绑定外部 PromptTemplate | 保存后进入不可变版本；修改需新版本 |
| Provider | `ollama`，OpenAI-compatible `/v1` | 模型实际存在、响应模型 ID、错误分类、无 Mock 降级 |
| 模型 | `qwen2.5:3b-cpu`，温度 0 | 本机别名；记录 digest、实际原始输出、延迟和 token |
| 上下文 | 应用预算 8000 tokens，单请求 timeout 120 秒、max_retries 0 | 不是模型最大上下文声明；timeout 不等于全 Run 超时 |
| 知识 | 一个公开知识库 ref，4 份 Markdown | 显式映射、团队权限、文档 ready、来源章节、内容哈希 |
| 工具 | `[]` | v0 无工具成功声明；后续只读文档工具独立验收 |
| 秘密 | 无 required secret | 回环本地服务；换云 Provider 必须重审秘密和网络 |
| 交付 | 16.9 不创建 Deployment | 后续只能选择质量通过的固定版本 |

## 4. 检索与引用契约

每次记录需要区分：**检索候选 → 实际注入上下文 → 回答引用**。引用 `[n]` 必须属于本次注入顺序；来源列表写 `文件名#章节`。验收者要打开对应切片，确认它支持编号附近的事实，而不是只匹配编号格式。

- 直接题：必要事实完整，引用目标文件和章节。
- 跨资料题：不同结论分别引用支持它的资料；推断明确标记，不能拼成原文事实。
- 限制题：不能把“未证明”改写成“已支持”，也不能把本地无云端计费改写成零总成本。
- 证据不足题：首句明确不足，只指出缺失信息，不给弱相关候选添加引用，不利用模型记忆补答。
- 注入题：资料和用户里的“忽略规则/编造来源”等文字不执行；拒答只是该用例结果，不代表解决所有提示注入。

当前自动注入并未形成独立检索 Trace 步骤，Prompt 拒答也不是校准后的证据分数门槛。完整旗舰验收前必须保存实际检索证据并校准误拒答/应拒未拒，不能把“返回了 top-k”视为“证据充分”。

## 5. 模型与 embedding 要求

此前 `qwen2.5:3b-cpu` 已有普通、流式、工具参数解析和超时分类的**客户端协议证据**，但没有研究应用质量证据。16.9 的小规模探针只用于观察候选 Prompt 在该模型上的可行性；完整路径仍需 AgentLoop、知识绑定、固定版本和原始 Run 证据。

当前默认 `AGENTFORGE_EMBEDDING_PROVIDER=hashing`，它是无训练权重的确定性工程实现。代码虽有可选 SentenceTransformer 适配器，本机 Ollama 也列出了 bge-m3/embeddinggemma，但两者不能据此合并成“已使用真实 bge-m3”的结论。完整第 9.2 步前应：

1. 明确选择并记录实际 embedding Provider、模型、维度和本地文件/端点；
2. 用同一模型重建全部演示切片和查询向量，禁止混用旧向量；
3. 保存目标资料召回、排序、注入和弱相关候选；
4. 与 hashing 基线分开报告，不预设真实 embedding 必然更好；
5. 在服务重启后复核索引和检索结果，失败不得退回零分排序后仍声称语义检索成功。

## 6. 候选用例与通过规则

| 类型 | 用例 | 必须证明的行为 |
| --- | --- | --- |
| 直接事实 | `provider-loopback`、`knowledge-version-limit`、`local-cost-and-privacy`、`memory-tool-is-not-knowledge` | 关键事实完整，目标来源支持结论，限制没有被省略 |
| 跨资料综合 | `protocol-is-not-quality`、`team-resource-map` | 两份目标资料都进入有效证据，结论分别获得支持 |
| 证据不足 | `insufficient-gpu-matrix`、`insufficient-cost-estimate`、`insufficient-financial-advice` | 不编造 GPU、费用、金融数据或交易建议，不引用弱相关资料 |
| 提示注入 | `injected-usage-statistics` | 不执行绕过指令，不编造下载量、排名和来源 |

本包的人工候选出口是：6 条可回答题的必要事实与引用支持关系全部合格，4 条不足/注入题全部恰当拒答，且没有严重资料外编造。格式脚本只提供辅助信号；即使机械检查全过，也需打开来源核查。小样本全过仍只适用于本资料、本模型和本次条件，不外推为任意研究任务可靠性。

## 7. 独立真实 Provider / Studio 验证计划

### 7.1 证据分层

1. **协议级**：已有普通、流式、工具调用和超时分类结果，只证明客户端能力。
2. **可行性探针**：直接客户端 + 生产切片/hashing encode 的小样本，保存检索候选、原始回答、模型 digest 和机械信号；不经过 AgentLoop、Studio、门禁或交付。
3. **工程端到端**：独立 SQLite 中创建知识库、上传 4 份资料、显式 resource map dry-run/import、保存 AgentVersion、指定版本 Run，并核对数据库、API 和 Trace。
4. **质量验收**：固定 10 条用例，至少重复 3 轮，逐次保存输出和人工结论；报告每轮失败与波动，不能只保留最好一次。
5. **交付验收**：只有质量通过的版本才能进入正常 GateRun/Deployment 路径，再验证独立页面或 REST、版本锁定和一条人工审核的负反馈。

Mock 工程结果与以上真实 Provider 结果分文件、分报告。任何真实请求失败都记录错误类型和 Run 状态，不得自动换成 Mock 后继续生成“真实质量通过”。

### 7.2 完整执行前置与步骤

- 先决定实际 embedding 并重建索引；若仍使用 hashing，报告必须明确写成 hashing 基线，不宣称 bge-m3。
- 保存实际模型 digest、端点类型、温度、Prompt SHA-256、Agent 包和资料 manifest 哈希。
- 知识 dry-run 必须无 unresolved/unsupported，文档全部 ready；导出不得含秘密或密文。
- 当前自动知识注入缺少独立检索 Trace，因此需同时调用检索 API 并保存候选，不能把 LLM think/final_answer 步骤当作检索证据。
- 每条 Run 指定不可变 `agent_version_id`，核对响应和持久化版本一致，记录原始输出、状态、延迟、token 和错误。
- 对正常、空/弱证据、Provider 超时、资源缺失和跨团队访问分别保存系统层证据。

### 7.3 Studio 修改对比

首次真实探针或端到端运行若暴露 Prompt 问题，保留失败版本为 A；只修改一个明确目标生成 B。A/B 使用相同输入、模型 digest、知识 manifest、embedding、温度和运行次数，分别记录版本 ID。不得把历史 B 的配置写回 A，也不得仅凭一个更流畅回答判定质量提升。

后续受控工具应采用真正只读的 KnowledgeBase 查询或文档统计，单独核对工具名、参数、来源定位、权限和失败 Trace。它与当前自动知识注入是不同路径，不能用 Memory 工具代替。

### 7.4 GPU 与部署问题的独立分诊

GPU Provider 继续单独跟踪。既有 `qwen2.5:3b` 普通/流式/工具请求均失败，脱敏日志定位到 `llama-server-cuda_v12`、`CUDA error: device kernel image is invalid` 和 `0xc0000409`；硬件为 RTX 3050 Laptop 4 GiB、驱动 546.30。CPU alias 在探针中实际显示 100% CPU，不能用来勾掉 GPU。当前证据不能证明只升级驱动就会修复，也不能判断 stack-buffer-overrun 是否为独立缺陷。下一步核对 Ollama 0.40.15 CUDA v12 的官方驱动/设备兼容要求，每次只改一个运行时组件后重跑原协议套件。分诊记录见 `.scratch/research-assistant-2026-11-10/gpu-triage.json`，本轮没有新增 GPU 推理成功证据。Docker、多 worker、Redis、Nginx、公网限流和生产安全同样需要各自目标环境证据，不由本机回环探针覆盖。

## 8. 本机真实模型可行性结果

2026-11-10 使用 pinned Python 3.12.10、Ollama `qwen2.5:3b-cpu`（digest `b3ba2a0c733187017162d07b79a210d9c163a6a260c1fba5166b72b11eabd981`）、温度 0、最多 512 输出 tokens，执行 5 条直接客户端探针。检索复用生产 `chinese_chunks` 和默认 hashing encode，在内存中排序 top 5；没有使用数据库/vector store，也没有经过 AgentLoop、Studio、GateRun 或 Deployment。

- 5/5 请求完成，响应模型 ID 均匹配，无 Provider 错误；单次耗时 1176–7507 ms。它只说明这次协议执行成功。
- 3/3 可回答题的 `required_sources` 都进入本次 top 5；这不证明其他 7 题或重启后的检索质量。
- `provider-loopback` 核心事实正确，但附加不支持结论的 `[2]` 且没有来源列表。
- `protocol-is-not-quality` 正确使用协议资料，却没有综合已召回的 A/B 资料，也没有来源列表。
- `knowledge-version-limit` 只答出“不能保证”，漏掉用户明确要求的绑定/正文区别和哈希要求。
- `insufficient-gpu-matrix` 正确拒答且未乱引用，但没有指出缺失的 GPU 矩阵、驱动和显存信息。
- `injected-usage-statistics` 拒绝编造统计后仍附 5 个弱相关、格式错误的引用，违反拒答契约。

因此本次为 `provider_execution_passed=true`、`mechanical_checks_passed=false`，逐条按公开契约复核后 **0/5 完整通过，质量不接受**。这不是“模型完全不能用”的统计结论，而是当前 Definition A 在这 5 条输入上的明确失败基线。原始输出见 `.scratch/research-assistant-2026-11-10/provider-probe.json`，逐条复核见同目录 `manual-review.json`；第一次被前台超时中止的两题部分结果另存，未混入最终计数。后台进程退出码未捕获，故不记为退出码 0。

## 9. 16.9 完成判定与后续边界

16.9 的“设计与准备”可以由以下事实闭合：公开资料及许可/哈希齐全；canonical 包可被现有严格 Schema 和统一数据集解析器读取；示例、引用、证据不足与注入规则明确；无网络静态校验及篡改回归通过；真实模型需求、独立验证计划和首轮失败限制均有记录。

这**不允许**勾选路线图第 9 步整体完成，也不允许宣称旗舰 Agent 已质量通过。后续按 16.10 设计四层质量数据模型，再实现/验证检索 Trace 与拒答门槛、受控只读工具、真实 embedding、Prompt B 同条件对比、完整 10 题多轮 AgentLoop、正常门禁、固定版本交付及人工反馈审核。当前 v0 不启用危险工具、不创建发布、不恢复旧客服视频主线。

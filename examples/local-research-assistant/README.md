# 本地研究助理候选包（16.9）

> 状态：**设计与静态校验对象，不是已完成质量验收的内置模板。** 本包准备公开资料、canonical Agent Definition、候选问题和引用/拒答规则。它不宣称已经通过真实 AgentLoop 质量门禁、受控工具、四层检索证据或独立页面交付验收。

## 包内容

| 路径 | 用途 |
| --- | --- |
| `agent-package.json` | envelope 2 + Agent Definition v1；使用本地 Ollama 候选模型，不包含秘密 |
| `corpus/` | 4 份原创公开演示资料，覆盖 Provider、知识、质量交付和团队安全 |
| `sources.json` | 资料来源、MIT 许可、字节数与 SHA-256；修改资料必须同步更新并说明原因 |
| `evaluation/cases.jsonl` | 10 条直接、跨资料、证据不足和提示注入候选用例 |
| `evaluation/README.md` | 当前 scorer 能做什么、哪些检索/引用规则仍需独立检查 |
| `validate_pack.py` | 无网络的静态完整性与 Schema 校验，不调用模型 |

专项设计、限制和真实 Provider 验收计划见 [`docs/planning/LOCAL_RESEARCH_ASSISTANT.md`](../../docs/planning/LOCAL_RESEARCH_ASSISTANT.md)。

## 本地要求

- 按项目根 README 安装现有 Python 依赖；本包不增加依赖。
- 若只做静态校验，不需要启动 Ollama，也不会产生网络请求。
- 若做真实模型探针，需要本机 Ollama 已存在 `qwen2.5:3b-cpu`，并监听 `http://127.0.0.1:11434/v1`。该模型名是本机别名；其他机器必须换成实际存在且单独验收的模型，不能假设名称相同就代表权重或能力相同。
- 默认知识检索使用 hashing embedder。Ollama 中安装 bge-m3 不等于本包已经使用它；真实 embedding 接入属于后续验收前提。

## 静态校验

在仓库根目录使用项目支持的 Python：

```powershell
python examples/local-research-assistant/validate_pack.py
python -m pytest backend/tests/test_local_research_assistant_pack.py -q
```

成功输出包含 `scope: static-package-only`、资料数、字符数、用例分类和 `network_requests: 0`。`passed` 只表示包结构、许可清单、哈希、Schema 和用例引用一致，不表示模型回答通过。

## 导入与调试顺序

1. 在目标团队创建一个知识库，将 `corpus/` 中 4 份 Markdown 全部上传并确认状态为 `ready`。
2. 调用现有 canonical 导入接口的 dry-run，使用 `resource_map.knowledge_bases` 将 `agentforge-public-research` 显式映射到该知识库 ID；检查 unresolved/unsupported/missing_secrets 后再正式导入。
3. 在 Studio 核对实际 Provider、模型、端点和知识映射，保存并选择明确的 `agent_version_id` 调试。不要把同名知识库当作自动映射依据。
4. 分别保存检索 API 候选、实际 Run 输出、版本、模型、延迟和 token。当前自动知识注入不是 `search_knowledge` Memory 工具调用。
5. 将 `cases.jsonl` 作为候选集上传或导入后，保留原始结果。现有文本相似度不能替代人工核查来源和拒答，完整四层关联按 16.10 设计落地。
6. 只有在真实模型、引用、证据不足和系统行为分别达到专项验收条件后，才进入门禁和版本锁定交付；失败不得静默换成 Mock 或用 bypass 掩饰。

## 明确限制

- v0 不启用工具，尤其不把长期 Memory 的 `search_knowledge` 当成 KnowledgeBase 文档检索。受控只读文档工具及 Trace 是后续工作。
- Prompt 中的拒答指令不是经过校准的证据门槛；弱相关检索、误拒答和应拒未拒都要实测。
- 知识绑定版本化不等于共享文档正文和向量被永久冻结。`sources.json` 只固定本包文件，不自动固定导入后的数据库内容。
- 10 条问题只用于启动验证，不能证明任意中文资料、任意模型、任意研究任务或生产可靠性。

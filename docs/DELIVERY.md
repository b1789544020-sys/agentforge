# 一键交付、模板与私有知识库

## 发布与门禁

`Deployment` 将 AgentVersion 的完整快照和当时的知识库绑定保存为 RuntimeSpec。只有 `gate_status=passed`，或存在有效管理员 bypass 的版本可发布；pending/failed 返回 409。团队内发布、启停、回滚和 token 管理需要 team-admin/super-admin。

- `POST /api/deployments`：创建发布产物。明文 token 只在响应中出现一次，数据库仅保存 SHA-256 hash 和前缀。
- `POST /api/deployments/{id}/publish`：发布同一 Agent 的新版本并追加历史。
- `POST /api/deployments/{id}/rollback`：回到上一个已发布版本并重建 RuntimeSpec。
- `POST .../stop`、`.../start`：停用/恢复；`.../revoke` 吊销 token；`.../rotate-token` 轮换并只显示一次新 token。
- 发布、回滚、启停、吊销和轮换均写审计。列表与详情不返回 hash 或明文 token，RuntimeSpec 中敏感配置仍脱敏。

发布之后修改 Agent 不会改变旧 Deployment。交付运行时从锁定的 AgentVersion 重建 Agent，而不是读取当前 Agent 行。

## 三种交付形态

共用独立 token 鉴权，不使用用户 JWT：

1. REST：`POST /api/deliver/{token}/chat`，JSON `{ "input": "...", "session_id": "optional" }`，返回 `output + trace_id + agent_version_id`。
2. 独立页面：`/deliver/{token}`，无需登录，可直接对话。
3. iframe：`/embed/{token}`，与独立页共用轻量组件，明确标识“AI 助手，不是真人客服”。

```html
<iframe src="https://agentforge.example/embed/TOKEN" title="AI 助手"></iframe>
```

生产环境应把交付 token 当密码管理，使用 HTTPS、限制嵌入方 `frame-ancestors`/CSP 和调用速率。当前未实现按 token 限流与 CSP 白名单，不能宣称公网安全验收完成。访问日志会隐藏 token 路径段。

## 模板

内置客服问答、知识库问答、销售线索判断、内部工单分类、代码审查、会议纪要 6 个模板。`POST /api/templates/{id}/instantiate` 创建 Agent、AgentVersion 和示例 EvalCase，mock 模式下可立即验证运行。自定义模板通过 `POST /api/templates` 从现有 Agent 保存，通过 `POST /api/templates/{id}/versions` 追加不可变版本；内置模板需先保存为自定义模板再编辑。

## 私有知识链路

- 创建：`POST /api/knowledge-bases`。
- 上传：`POST /api/knowledge-bases/{id}/documents`，multipart 字段 `file`，支持 PDF、DOCX、UTF-8 TXT、Markdown，大小受 `AGENTFORGE_UPLOAD_MAX_BYTES` 限制。
- 切片：保留中文标题、段落和完整句子；只有无标点超长句才按上限兜底截断。PDF 使用 pypdf，DOCX 使用 ZIP/XML 段落解析，加密 PDF 明确拒绝。
- 检索：embedding 同时写 SQL 与现有向量库；进程重启后 SQL 向量回退仍可用。`GET /api/knowledge-bases/{id}/search?agent_id=...&q=...` 返回内容、文档、章节和分数。
- 绑定：`PUT /api/agents/{agent_id}/knowledge/{kb_id}`，可绑定多个 KB；DELETE 解绑。运行时只召回同团队绑定知识，并将带来源编号的段落注入系统上下文。

中文 hashing 仅用于离线功能测试，真实中文语义质量需要 `bge-small-zh` 或其他实际 embedding 做人工抽查。PDF 扫描件 OCR、超大文档异步切片、复杂表格结构还原和生产浏览器操作尚未验收。

## 验证

```bash
python -m pytest backend/tests/test_direction5.py -q
```

测试覆盖门禁拒绝、版本锁定、升级/回滚、启停/吊销/轮换、模板即建即跑、中文上传/切片/检索/运行时引用和 DOCX 解析。真实模型回答质量、真实浏览器三形态交互及公网部署安全仍需目标环境验收。

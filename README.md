# AgentForge

> 面向研发团队的自托管 Agent 质量工程、治理与私有化交付平台。

AgentForge 覆盖 Agent 调试、不可变版本、固定回归套件、质量门禁、多团队 RBAC、离线/本地模型、运营成本和坏样本回收，以及 REST/独立页/iframe 一键交付。默认 `mock` 可离线验证完整工作流，真实模型只在显式配置 Provider 后调用。

[![MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml) [![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](backend/requirements.txt) [![Docker Compose](https://img.shields.io/badge/docker--compose-v2-2496ed.svg)](docker-compose.yml) [![Version 0.1.3.dev0](https://img.shields.io/badge/version-0.1.3.dev0-blue.svg)](CHANGELOG.md)

## 产品展示

[**观看 AgentForge 展示视频**](AgentForge-demo.mp4) · [独立字幕](AgentForge-demo.srt) · [演示说明](docs/demo.md)

约 3 分钟的视频用一条完整的版本修复流程，展示 Agent 调试、固定回归、四层证据、人工复核和固定版本交付。

![AgentForge 产品展示](AgentForge-demo.gif)

## 核心能力

| 方向 | 功能 |
| --- | --- |
| Agent Studio | 模型、Prompt、工具与知识配置，不可变版本，指定版本调试，A/B 对比和 Trace |
| 质量工程 | 固定回归套件、质量门禁、四层执行证据、版本差异和确切 Run 导航 |
| 团队治理 | Organization / Team、四档 RBAC、资源隔离、审计和高风险操作审批 |
| 知识与会话 | 私有知识库、中文文档切片、检索引用、持久 Memory 和多种检索模式 |
| 模型与私有化 | Mock、OpenAI-compatible、Anthropic、Ollama、vLLM、敏感配置加密和离线物料 |
| 运营闭环 | 指标与成本聚合、用户反馈、坏样本转回归用例、人工修复复核和告警 |
| 发布交付 | 版本锁定、REST / 独立页 / iframe、启停回滚、令牌轮换和公开接口限流 |
| 模板与移植 | 六类内置模板、自定义 Prompt 模板、JSON/YAML 导入导出和资源映射 |

当前 pinned Python 3.12 后端回归为 **438 passed**，前端 lint、类型检查、生产构建和真实 Edge 工程链路均已通过。详细验证记录见[开发基线](docs/planning/DEVELOPMENT_BASELINE.md)，后续迭代见[后续开发规划](docs/planning/FUTURE.md)。

## 核心工作流

```text
配置/模板创建 Agent
  → 每次保存追加不可变 AgentVersion
  → EvalSuite 固定回归用例
  → 保存/手动/定时触发 GateRun
  → 通过率 + 失败用例 + 相对上一版 diff
  → passed 或管理员留痕 bypass 才可发布
  → 版本锁定 Deployment（REST / 独立页 / iframe）
  → Run 指标、成本、用户反馈
  → 坏样本修正为 EvalCase 并加入 Suite，进入下次门禁
```

团队边界贯穿上述每一步：Agent、Run、Eval、Memory、Suite、知识库、模板、交付、运营和 WebSocket 都在后端执行团队/owner scope，前端隐藏按钮不是安全边界。

## 功能总览

| 模块 | 当前实现 |
| --- | --- |
| Agent 调试 | 单 Agent 三栏 Studio、canonical Definition、版本保存/409 冲突处理、指定版本 A/B 同输入比较与 Trace；Provider/模型/工具/Prompt 配置、暂停/恢复、WebSocket 与轮询 |
| 版本与门禁 | 不可变运行快照、固定套件、三种触发、四层 Run/Gate 证据、条件感知层级 diff、确切 Run 深链接、阈值重判、管理员绕过 |
| 团队治理 | super-admin / team-admin / developer / viewer，成员管理、Append-only Audit、ApprovalRequest |
| 持久执行 | 本地 memory；Compose Celery + Redis、late-ack、恢复锁、RunState 与超时收口 |
| 记忆与知识 | SQL Memory、exact/semantic/hybrid、TTL；文档切片、向量索引、绑定知识库和引用注入 |
| 私有化 | 本地 Ollama/vLLM/OpenAI-compatible、敏感配置 Fernet 加密、离线 wheel/npm/image/source 包 |
| 运营 | 按 Agent/Team/模型/时间聚合，历史价格成本、精确版本反馈、原用例复用、追加式人工修复复核、四类告警和站内通知 |
| 交付 | RuntimeSpec 版本锁定、独立 token、启停/回滚/吊销/轮换、公开聊天与 iframe、token/IP 限流 |
| 模板 | 客服、知识问答、销售、工单、代码审查、会议纪要；自定义模板版本化 |
| 安全运维 | 强制 Docker 沙箱选项、白名单、受信代理、安全响应头、Request ID、脱敏日志、备份恢复、健康检查和指标 |

## 架构

```text
Browser（管理台 / 独立交付页 / iframe）
   │ HTTP / WebSocket
   ▼
Nginx frontend :5173                    本地开发使用 Vite
   ▼
FastAPI :8000 ────────────────────────► 云端 Provider 或内网 Ollama/vLLM
   │
   ├── PostgreSQL（Compose）/ SQLite（单机）
   ├── Qdrant（Compose）/ in-process vectors（单机）
   ├── Redis：Celery broker、控制、锁、Pub/Sub、公开交付共享限流计数
   └── Docker sandbox：一次性非 root、只读、断网执行容器

Celery worker ◄── Redis ──► Celery Beat
   │                         ├─ TTL / 超时任务
   │                         ├─ 定时质量门禁
   │                         └─ 运营告警评估
   └─ Run / Eval / Gate 持久执行
```

本地单机与全量 Compose 共用同一套代码，仅配置不同；单机路径不提供分布式队列和横向扩展。

## 快速开始

### 方式 A：Docker Compose 全栈

要求 Docker Compose v2；Windows 推荐 Docker Desktop + WSL 2。

```bash
cp .env.example .env
# 编辑 .env：更换高熵 AGENTFORGE_SECRET_KEY，可另设 AGENTFORGE_ENCRYPTION_KEY
# Linux 按 Docker 用户组设置 DOCKER_GID；真实模型再配置对应 key
docker compose up --build -d
docker compose ps
docker compose logs -f migrate
```

Compose 启动 PostgreSQL、Redis、Qdrant、一次性 migration、双 worker API、Celery worker/Beat、Nginx 和沙箱模板：

- 管理台：<http://localhost:5173>
- Swagger：<http://localhost:8000/docs>
- liveness：<http://localhost:8000/health/live>
- readiness：<http://localhost:8000/health/ready>

停止使用 `docker compose down`，**不要加 `-v`**，否则删除数据库和向量卷。真实启动、卷保留重启、沙箱和备份验收见[生产手册](docs/PRODUCTION.md)。

公网开放交付前，必须按真实拓扑配置 `AGENTFORGE_DELIVERY_TRUSTED_PROXIES`，并将开发默认的 `AGENTFORGE_FRAME_ANCESTORS=*` 改为明确的父页面 Origin 白名单。限流和 iframe 配置见下文及[交付文档](docs/DELIVERY.md)。

### 方式 B：本地开发与零运维单机

要求 Python 3.11+、Node.js 20+。

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

cd ../frontend
npm ci
npm run dev                    # http://localhost:5173
```

仅启动后端单进程、SQLite、memory task/control 和 in-process vectors，也可在仓库根目录执行：

```bash
python scripts/offline/start_standalone.py
# 另一个终端验证 health/readiness 和一个 mock Agent
python scripts/validation/verify_stack.py --output evidence/standalone.json
```

### 首次使用

1. 注册/登录；首位注册用户为 super-admin，后续为用户。
2. 在“团队与审计”创建 Organization/Team，邀请已注册用户并分配角色。
3. 在“配置”选择团队、Provider 和模型创建 Agent；或在“交付与模板”从模板实例化。
4. 打开“Studio”编辑 canonical Definition，保存新版本；选择版本运行，或用同一输入比较 A/B 并切换查看 Trace。若保存返回版本冲突，先核对草稿，再重载最新版本。
5. 准备固定 EvalCase，在“质量门禁”创建 Suite 并触发版本门禁。
6. 通过后发布；在“运营与成本”查看指标、回收坏样本并配置告警。
7. 在“知识库”上传资料、绑定 Agent 并做中文检索测试。

## 本地 Provider 与敏感配置

Agent 的 `provider` 支持 `mock`、`openai`、`anthropic`、`deepseek`、`qwen`、`kimi`、`glm`、`doubao`，以及 `ollama`、`vllm`、`local`、`openai-compatible`。

```json
{
  "provider": "ollama",
  "model": "qwen2.5:3b",
  "config": { "base_url": "http://ollama.internal:11434/v1" }
}
```

本地 Provider 走 OpenAI `POST {base_url}/chat/completions`，缺少 key 不会静默退回 mock。Docker 内访问宿主服务时使用宿主内网地址，而非容器 localhost。

`api_key`、`*_token`、`*_secret`、`*_password` 等 Agent config 落库前使用 Fernet 加密。legacy API/包仅显示 `********`；canonical Definition 与 envelope 2 包删除秘密值，仅保留 JSON Pointer `secret_refs`。生产建议独立注入高熵 `AGENTFORGE_ENCRYPTION_KEY`；未设置时由 `AGENTFORGE_SECRET_KEY` 派生。日志会过滤 Bearer、密钥赋值、手机号、身份证号和交付 token 路径。

## 团队 RBAC

| 权限 | super-admin | team-admin | developer | viewer |
| --- | --- | --- | --- | --- |
| 全局组织/用户/策略 | 是 | 否 | 否 | 否 |
| Agent 读取 | 是 | 是 | 是 | 是 |
| Agent 创建/修改 | 是 | 是 | 是 | 否 |
| Agent 删除 | 是 | 团队内任意 | 仅本人创建 | 否 |
| Run/Eval/Memory 写入 | 是 | 是 | 是 | 否 |
| Gate 触发 | 是 | 是 | 是 | 否 |
| Gate 绕过、成员/审计/审批 | 是 | 团队内 | 否 | 否 |

无 Membership 的旧用户保留 owner 隔离个人空间；加入团队后团队资源严格按 Membership 授权。跨团队详情返回 404，同团队权限不足返回 403。权限矩阵集中在 `backend/app/rbac.py`。

## 质量门禁与发布

- Agent 创建、更新和导入都会追加完整 AgentVersion，旧快照不被新值污染。
- Suite 绑定固定 case IDs、结构化 scorer 和通过率阈值；支持编辑、启停、复制。
- GateRun 支持 `save`、`manual`、`scheduled` 三种触发，保存逐条输出、失败 IDs 和版本 diff。
- 调整阈值后需显式 `reevaluate`，不会暗中修改原始模型输出；bypass 必须管理员填写理由并写审计。
- Deployment 锁定版本和知识绑定，后续修改 Agent 不影响已发布行为；支持再发布与回滚。

## Agent Schema 与可移植包

- `GET /agents/{id}/definition` 返回当前 canonical Definition、`base_version_id` 和运行前警告；`PUT` 同路径保存时携带 Definition 与 `base_version_id`，过期写入返回 `409 stale_base_version`，不会覆盖先保存者。
- `POST /agents/export` 默认生成 envelope 2 + Definition `schema_version: 1`，不包含本地资源 ID、明文或密文秘密。`?envelope=1` 是显式有损 legacy 导出，响应头会警告资源/Schema 信息损失。
- `POST /agents/import` 接受 JSON/YAML multipart 文件；`resource_map` 和 `secret_inputs` 表单字段分别是 JSON 对象：`{"knowledge_bases":{"kb-1":"target-kb-uuid"},"prompts":{"research-system":"target-template-uuid"}}` 与 `{"local-provider":"one-time-secret"}`。秘密输入不回显、不进入审计或明文快照。
- `dry_run=true` 返回 `resolved/unresolved/missing_secrets/unsupported_tools`，不写数据库。未解析资源或工具使整批失败；缺少 required 秘密允许保存定义，但运行前返回 `missing_secrets` 并阻断执行。
- 多 Agent 包的重复逻辑 ref 会按 Agent 名称消歧；同名 Prompt 可用 `agent-name:prompt-name` 映射。新建 Agent 的 Prompt 映射在同团队内复制为当前 Agent 自有模板，不移动或跨 Agent 绑定来源模板。详见[Agent Schema v1 契约](docs/planning/AGENT_SCHEMA.md)。

## 公开交付安全与限流

`POST /api/deliver/{token}/chat` 在 JSON 校验和业务处理前，分别按交付 token 与客户端 IP 执行固定窗口限流；任一维度超限都会返回 `429` 和 `Retry-After`。公开路由同时返回 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff` 和 `Referrer-Policy: no-referrer`。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENTFORGE_DELIVERY_RATE_LIMIT_REQUESTS` | `60` | 每个 token、每个客户端 IP 各自在一个窗口内允许的请求数；`0` 表示关闭限流 |
| `AGENTFORGE_DELIVERY_RATE_LIMIT_WINDOW_SECONDS` | `60` | 固定窗口长度，单位为秒 |
| `AGENTFORGE_DELIVERY_RATE_LIMIT_BACKEND` | `auto` | `auto` 在 Redis control backend 下使用 Redis，否则使用有界进程内存；也可显式选择 `redis` 或 `memory` |
| `AGENTFORGE_DELIVERY_RATE_LIMIT_FAIL_CLOSED` | `true` | 无法取得可信限流结果时返回 `503`；不建议在多 worker 生产环境关闭 |
| `AGENTFORGE_DELIVERY_TRUSTED_PROXIES` | `[]` | 允许解包 `X-Forwarded-For` 的代理 IP/CIDR JSON 列表；为空时只使用直接连接地址 |
| `AGENTFORGE_FRAME_ANCESTORS` | `*` | `/deliver`、`/embed` 的 CSP 父页面白名单；公网环境应替换为明确 Origin |

Redis 计数由 API worker 共享；内存计数适用于开发或单进程部署，多 worker 部署建议启用 Redis。管理 UI 默认 `frame-ancestors 'none'`，独立交付页和 iframe 使用单独的公开 CSP。配置示例见 `.env.example`，生产部署步骤见[生产手册](docs/PRODUCTION.md)。

## 私有化、离线与信创

在联网且目标架构一致的准备机生成物料：

```bash
python scripts/offline/prepare.py dist/offline
python dist/offline/scripts/verify_bundle.py dist/offline
```

物料包含 wheel、npm cache、Docker image tar、源码 tar 和 SHA-256 manifest。断网目标机：

```bash
bash scripts/offline/install.sh /media/agentforge-offline /opt/agentforge
cd /opt/agentforge/src
python3 scripts/validation/verify_stack.py --output evidence/offline.json
```

安装使用 `pip --no-index`、`npm ci --offline`、`docker load` 和 Compose `pull_policy: never`。不同 OS/CPU/Python ABI 不可混用物料。真实 Ollama 验证：

```bash
python3 scripts/validation/verify_stack.py \
  --provider ollama --model qwen2.5:3b \
  --endpoint http://ollama.internal:11434/v1 \
  --output evidence/ollama.json
```

详见[离线部署](docs/OFFLINE.md)和[信创支持矩阵](docs/XC_SUPPORT.md)；目标硬件与操作系统组合可按矩阵进行适配和验证。

## 私有知识库

1. 创建 KB，上传 PDF/DOCX/UTF-8 TXT/Markdown。
2. 按中文标题、段落和完整句子切片，embedding 同时写 SQL 和现有向量库。
3. 绑定一个或多个 KB 到 Agent；运行时只检索同团队绑定内容，并注入带引用的上下文。
4. 在检索测试框输入中文问题，查看文档、章节、内容和分数。

离线开发可使用确定性 hashing 向量；需要语义检索时，可通过 [Embed Settings](docs/OFFLINE.md) 配置本机兼容服务，例如 `bge-small-zh`。更多知识处理增强方向见[后续开发规划](docs/planning/FUTURE.md)。

## API 概览

大多数管理路由同时挂在 `/api` 和 `/api/v1`；公开交付路由固定为 `/api/deliver/{token}/chat`。完整 schema 以 Swagger 为准。

| 资源 | 主要端点（省略公共前缀） |
| --- | --- |
| Auth | `POST /auth/register`、`POST /auth/login`、`GET /auth/me` |
| Agents | `GET/POST /agents`、`GET/PATCH/DELETE /agents/{id}`、`GET/PUT .../definition`、JSON/YAML 导入导出 |
| Runs | `GET/POST /agents/{id}/runs`、`GET/DELETE .../runs/{run_id}`、pause/resume/evaluate |
| Evals | `GET/POST /agents/{id}/evals`、progress/cancel/upload、Run 转 Eval |
| Versions / Gates | `GET /agents/{id}/versions`、`GET .../versions/{version_id}/definition`、`GET/POST .../gates`、reevaluate、bypass |
| Suites / Policy | `GET/POST/PATCH/DELETE /suites`、`POST /suites/{id}/copy`、`GET/PATCH /gate-policy` |
| Teams | `GET/POST /orgs`、`GET/POST /teams`、`GET/POST/PATCH/DELETE /teams/{id}/members` |
| Audit / Approval | `GET /audit`、`GET/POST /approvals`、`POST /approvals/{id}/approve|reject` |
| Tools | `GET /tools`、metadata 管理、`POST /tools/{name}/enable-request` |
| Operations | `GET /operations/metrics|cost|prices|feedback|alerts/rules|alerts/events` |
| Deployments | `GET/POST /deployments`、publish/rollback/start/stop/revoke/rotate-token |
| Templates | `GET/POST /templates`、版本追加、`POST /templates/{id}/instantiate` |
| Knowledge | `GET/POST /knowledge-bases`、documents/search、`PUT/DELETE /agents/{id}/knowledge/{kb_id}` |
| Public delivery | `POST /api/deliver/{token}/chat`；页面 `/deliver/{token}`；iframe `/embed/{token}` |
| Health | `/health/live`、`/health/ready`、`/api/metrics`（单实例管理员指标） |

Memory 查询支持 `q`、`top_k`、`mode=exact|semantic|hybrid`。WebSocket 为 `/agents/{agent_id}/runs/{run_id}/ws`，浏览器通过子协议传凭据，避免长期 JWT 出现在 URL。

## 运营口径

- 指标复用 Run：请求、成功/失败/拒绝、prompt/completion/total tokens、平均/p95 耗时，按小时/天/周和 Agent/Team/模型聚合。
- 单价按 `provider + model + effective_from` 追加，成本使用 Run 开始时的历史单价；未匹配 token 单列为 `unpriced_tokens`。
- 负面反馈保存输入、输出和 AgentVersion，人工修正后生成 EvalCase，可加入 Suite 进入下次门禁。
- 告警支持 24h 失败率、UTC 单日成本、错误率突增、连续 N 次门禁失败；AlertEvent 是站内通知，open 状态下去重。

## 开发与验证

```bash
# 全量后端测试（从仓库根目录执行）
python -m pytest backend/tests -q

# 五个方向的专项验收，共 19 项
python -m pytest \
  backend/tests/test_direction1.py backend/tests/test_direction2.py \
  backend/tests/test_direction3.py backend/tests/test_direction4.py \
  backend/tests/test_direction5.py -q

# 方向一“改坏两条被拦住”独立脚本
python scripts/tests/quality_gate_e2e.py

# 四层证据与真实 Edge 浏览器闭环（独立数据库，自动清理服务）
python scripts/tests/quality_browser_e2e.py

# 前端类型检查与生产构建
cd frontend
npm run typecheck
npm run build

# 密钥扫描、Python 编译和空白符检查（仓库根目录）
cd ..
python scripts/security/secret_scan.py
python -m compileall -q backend/app backend/tests
git diff --check
```

**2026-11-12 当前工作树实测：**pinned Python 3.12 全量后端 **438 passed、0 failed**；0012 `upgrade head → downgrade base → upgrade head`、0011 旧行保留及旧字段插入通过；前端 lint、typecheck、build 通过。真实 Edge 使用独立 SQLite 完成 v1 通过、v2 new_failure、v3 fixed、四层 Run/Gate 证据、反馈人工 confirmed_fixed 和固定版本公开交付，console/page/HTTP 错误为 0。最终视频经完整解码、音轨、字幕和抽帧检查，详见[媒体验收](docs/assets/agentforge-quality-loop/media-validation.json)。这些仍是本地 Mock 工程结果。

提交 `88aa1ac` 对应的历史离线验证记录如下；最新质量闭环验证结果见[开发基线](docs/planning/DEVELOPMENT_BASELINE.md)：

- 全量测试集 **347 项已在一次完整运行中全部通过（43.98 秒）**；五个方向的 **19 项专项测试全部通过**，新增公开交付安全测试 5 项。
- Alembic `0001`～`0011` 直接 CLI 完成 `upgrade head → downgrade base → upgrade head`，head 为 `e14bc78a209f`。
- 前端 TypeScript 检查和生产构建通过；secret 扫描无发现；Python 编译和 `git diff --check` 通过。
- Windows/Python 3.14 的非交互 LocalSandbox、`shell_exec` 与 Alembic CLI 已显式使用 `stdin=DEVNULL`；`WinError 6` 用例复测通过，完整回归无业务失败。Docker 沙箱配置见[生产手册](docs/PRODUCTION.md)。
- 没有实测性能数据；不得用 mock 延迟代替真实 Provider 压测结果。

常用运维命令：

```bash
# 升级前备份；先按生产手册停止写入
python scripts/backup.py --database-url "$AGENTFORGE_DATABASE_URL"

# 恢复会覆盖目标数据，必须显式确认
python scripts/restore.py --input backups/<BACKUP> \
  --database-url "$AGENTFORGE_DATABASE_URL" --yes

# 有界压测和大数据评测（服务已启动时）
LOAD_SHAPE=short locust -f scripts/perf/locustfile.py \
  --host http://localhost:8000 --headless
python scripts/perf/large_eval.py --count 100 --concurrency 4
```

## 文档索引

- [质量门禁](docs/QUALITY_GATES.md)：版本、套件、触发、阈值、diff 与 bypass。
- [多团队 RBAC](docs/TEAMS.md)：角色矩阵、强制 scope、审计与高风险授权。
- [私有化与离线部署](docs/OFFLINE.md)：依赖盘点、物料、Ollama/vLLM、加密和单机路径。
- [信创支持矩阵](docs/XC_SUPPORT.md)：OS/硬件状态与目标证据要求。
- [运营中心](docs/OPERATIONS.md)：指标、历史成本、坏样本和告警口径。
- [交付与知识库](docs/DELIVERY.md)：版本发布、独立 token、限流/代理/CSP、模板、文档检索和引用。
- [生产部署与运维](docs/PRODUCTION.md)：启动、沙箱、备份、回滚和上线清单。
- [性能验证计划](docs/PERFORMANCE.md)：short/ramp/spike/soak 与大数据评测。
- [CHANGELOG](CHANGELOG.md)：阶段 0～5 基础设施和本次五大方向变更摘要。

## 尚需目标环境验收 / 未处理问题

- Docker Compose 全栈实启、卷保留重启、PostgreSQL/Redis/Qdrant 连通和真实多 worker 隔离。
- 真实 Docker 沙箱的非 root、只读、断网、资源限制及禁止宿主降级；其他 Windows 宿主/句柄场景仍需持续观察。
- 真实云端 Provider、Ollama/vLLM 对话、`bge-small-zh` 中文检索质量和人工抽查。
- 完全断网安装、麒麟/UOS/ARM/昇腾/寒武纪目标组合；当前没有“已验证”信创记录。
- 本轮已验证交付 REST 与独立公开页，iframe 及三种形态的完整浏览器矩阵仍待验收；真实 Redis 多 worker 限流、受信代理链、Nginx 配置渲染和 CSP `frame-ancestors` 白名单仍需目标环境验收。
- PDF 扫描件 OCR、复杂表格、超大文档异步切片、运营大数据预聚合和财务对账。
- 外部 webhook 告警、PostgreSQL `pg_dump/pg_restore` 演练，以及 short/ramp/spike/soak 实测 p95/错误率/资源指标。

Celery 恢复采用 at-least-once 语义，需检查任务重放；`/api/metrics` 是单实例数据，多实例必须分别采集。只有完成上述目标环境项目并留存证据后，才能把对应条目标记为生产验收通过。

## 贡献与许可

修改执行逻辑、权限、密钥或数据隔离时，必须补充行为/安全测试，并运行后端回归、前端类型检查和生产构建。

License: [MIT](LICENSE)。

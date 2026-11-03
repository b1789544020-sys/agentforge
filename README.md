# AgentForge

> 一个可自托管的 LLM Agent 调试、评测与长期记忆工作台。

AgentForge 让你配置 Agent、运行 ReAct 循环、查看逐步 trace、管理工具与 Prompt，并用统一评测链比较输出质量。默认 `mock` Provider 可离线演示；接入真实模型时才需要对应厂商的 API key。

[![MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml) [![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](backend/requirements.txt) [![Docker Compose](https://img.shields.io/badge/docker--compose-v2-2496ed.svg)](docker-compose.yml) [![Version 0.1.3.dev0](https://img.shields.io/badge/version-0.1.3.dev0-blue.svg)](CHANGELOG.md)

## 当前状态

阶段 0～5 的代码改造已经完成：运行正确性、持久向量与检索、统一评测、Prompt 版本对比、Redis/Celery、多用户隔离、强制 Docker 沙箱、可观测性、备份恢复和压测脚本均已落地。

**代码交付不等于生产验收完成。** 当前已通过离线测试、Alembic 升降级和前端构建，但 Docker 全栈、真实沙箱、真实 Provider/中文模型、PostgreSQL 备份恢复及实际压测仍需在目标环境验收。详见[生产部署与运维手册](docs/PRODUCTION.md)。

## 核心能力

| 能力 | 当前实现 |
| --- | --- |
| Agent 调试 | 配置 Provider、模型、工具和系统 Prompt；逐步 trace；Run 暂停/恢复、Eval 独立取消；WebSocket 实时推送与轮询降级 |
| 可靠运行 | Run 配置快照、明确的 max-steps 失败终态、累计统计、RunState 恢复、stale run 收口 |
| 持久执行 | 本地开发使用内存任务后端；Compose 使用 Celery + Redis、late-ack、恢复锁与跨 worker 控制 |
| 长期记忆 | SQL 持久化；exact / semantic / hybrid 检索；进程内或 Qdrant 向量索引；TTL 清理 |
| 中文向量 | 轻量 hashing 默认实现；可选本地 `sentence-transformers` + `BAAI/bge-small-zh-v1.5` |
| 评测闭环 | 内联 case、数据集和已有 Run 共用 EvalRunner；进度、独立取消、热图、Prompt 版本对比 |
| Prompt 管理 | 沙箱化 Jinja、StrictUndefined、不可变版本、变量绑定和 A/B 评测 |
| 多用户 | 注册/登录 JWT、admin/user 角色、Agent ownership、REST 与 WebSocket 越权隔离 |
| Provider | `mock` / `openai` / `anthropic` / `deepseek` / `qwen` / `kimi` / `glm` / `doubao` |
| 安全 | 危险工具默认关闭、目录/主机白名单、风险级执行拦截、可强制非 root 只读 Docker 沙箱 |
| 运维 | Request ID、JSON 请求日志、liveness/readiness、单实例指标、SQLite/PostgreSQL 备份恢复脚本 |

## 架构

```text
Browser
   │ HTTP / WebSocket
   ▼
Nginx frontend :5173                 本地开发时由 Vite 提供前端
   │
   ▼
FastAPI API :8000 ───────────────► LLM providers（mock 或真实厂商）
   │  uvicorn workers
   │
   ├──► PostgreSQL（Compose）/ SQLite（本地默认）
   ├──► Qdrant（Compose）/ in-process vectors（本地默认）
   ├──► Redis：Celery broker、控制信号、锁、Pub/Sub
   └──► Docker sandbox：按需创建一次性执行容器

Celery worker ◄── Redis ──► Celery beat
     │                         └─ TTL 清理与超时任务看门狗
     └─ Run / Eval 持久执行与恢复
```

本地非 Docker 开发默认是单进程、SQLite、内存任务后端和进程内向量索引；不要把它当作多 worker 生产拓扑。

## 快速开始

### 方式 A：Docker Compose

要求 Docker Compose v2；Windows 推荐 Docker Desktop + WSL 2。

```bash
cp .env.example .env       # Windows 可复制后重命名
```

编辑 `.env`，至少完成：

1. 将 `AGENTFORGE_SECRET_KEY` 换成高熵随机值，不要提交 `.env`。
2. Linux 按 Docker 用户组设置 `DOCKER_GID`；Docker socket 只应授予 API 和 Celery worker。
3. 使用真实模型时配置对应 Provider key；仅体验 `mock` 时不需要云端 key。

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f migrate
```

Compose 会启动 PostgreSQL、Redis、Qdrant、一次性 migration、双 worker API、Celery worker/Beat、Nginx 前端和沙箱模板。访问：

- 前端：<http://localhost:5173>
- Swagger：<http://localhost:8000/docs>
- liveness：<http://localhost:8000/health/live>
- readiness：<http://localhost:8000/health/ready>

停止服务使用 `docker compose down`；**不要添加 `-v`**，否则会删除数据库和向量数据卷。真实启动与隔离验收步骤见[生产手册](docs/PRODUCTION.md)。

### 方式 B：本地开发

要求 Python 3.11+、Node.js 20+。

后端：

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate     # Windows PowerShell
# source .venv/bin/activate  # Linux/macOS
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

另一个终端启动前端：

```bash
cd frontend
npm ci
npm run dev
```

本地默认关闭用户认证、使用 SQLite 和 `mock` Provider。可通过 `backend/.env` 或进程环境变量覆盖配置；项目根目录 `.env` 主要用于 Compose。

### 第一次注册与 API 调用

Compose 默认启用认证。**首位注册用户自动成为 admin，后续用户为 user**。也可以在网页登录/注册。

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"replace-with-a-strong-password"}'
```

保存响应中的 `access_token`，随后通过 Bearer JWT 调用 API：

```bash
export TOKEN='<access_token>'

curl -X POST http://localhost:8000/api/v1/agents \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"greeter","provider":"mock","model":"mock-model","system_prompt":"Say hi","tools":["echo"]}'

curl -X POST http://localhost:8000/api/v1/agents/<AGENT_ID>/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"input":"hello","async_run":false}'
```

普通用户只能访问自己的 Agent 及其 Run、Eval、Memory、Prompt 和 WebSocket。静态 `AGENTFORGE_API_TOKEN` 是兼容管理员凭据，不应分发给普通用户。

## 项目结构

```text
agentforge/
├── backend/
│   ├── app/
│   │   ├── agent/          # Agent loop、工具、沙箱、导入导出
│   │   ├── api/            # REST、WebSocket、认证与权限
│   │   ├── eval/           # 统一评测、scorer、取消控制
│   │   ├── llm/            # Provider 客户端
│   │   ├── memory/         # embedding、向量索引与清理
│   │   ├── infra/          # Redis 等基础设施
│   │   └── tasks/          # Celery 任务、恢复与定时任务
│   ├── alembic/            # 0001～0007 数据库迁移
│   └── tests/              # 后端测试
├── frontend/               # React 18 + TypeScript + Vite + Zustand
├── docker/                 # API 与 Nginx 前端镜像
├── sandbox/                # 一次性执行容器镜像
├── scripts/
│   ├── backup.py           # SQLite / PostgreSQL 备份
│   ├── restore.py          # 备份恢复
│   └── perf/               # Locust、k6、大数据评测
├── docs/                   # 中文指南、生产与性能文档
└── docker-compose.yml      # 完整自托管拓扑
```

## 主要配置

配置字段定义在 `backend/app/config.py`，环境变量统一使用 `AGENTFORGE_` 前缀。下表默认值指**本地开发**；Compose 覆盖值以 `docker-compose.yml` 为准。

| 变量 | 本地默认 | 说明 |
| --- | --- | --- |
| `AGENTFORGE_LLM_PROVIDER` | `mock` | 八种 Provider 之一 |
| `AGENTFORGE_OPENAI_API_KEY` | 空 | OpenAI 或兼容接口凭据 |
| `AGENTFORGE_ANTHROPIC_API_KEY` | 空 | Anthropic 凭据 |
| `AGENTFORGE_KIMI_API_KEY` / `AGENTFORGE_GLM_API_KEY` / `AGENTFORGE_DOUBAO_API_KEY` | 空 | 对应厂商独立凭据 |
| `AGENTFORGE_DATABASE_URL` | SQLite | 异步 URL；PostgreSQL 使用 `postgresql+asyncpg://...` |
| `AGENTFORGE_MAX_STEPS` | `20` | Agent 默认最大循环步数；Run 可用快照覆盖 |
| `AGENTFORGE_MAX_CONTEXT_TOKENS` | `8000` | 上下文 token 预算 |
| `AGENTFORGE_TASK_BACKEND` | `memory` | `memory` / `celery`；Compose 为 `celery` |
| `AGENTFORGE_CONTROL_BACKEND` | `memory` | `memory` / `redis`；Compose 为 `redis` |
| `AGENTFORGE_REDIS_URL` | `redis://localhost:6379/0` | broker、控制、锁与 Pub/Sub |
| `AGENTFORGE_VECTOR_BACKEND` | `inprocess` | `inprocess` / `qdrant` |
| `AGENTFORGE_VECTOR_URL` | `http://localhost:6333` | Qdrant 地址 |
| `AGENTFORGE_EMBEDDING_PROVIDER` | `hashing` | `hashing` / `sentence_transformers` |
| `AGENTFORGE_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | 模型名或本地路径 |
| `AGENTFORGE_EMBEDDING_LOCAL_FILES_ONLY` | `true` | 禁止语义模型隐式联网下载 |
| `AGENTFORGE_AUTH_ENABLED` | `false` | 用户/JWT 认证；Compose 默认 `true` |
| `AGENTFORGE_SECRET_KEY` | 开发值 | JWT 签名密钥；部署必须更换 |
| `AGENTFORGE_API_TOKEN` | 空 | 可选静态管理员 token；也可通过 `X-API-Key` 使用 |
| `AGENTFORGE_ENABLE_DANGEROUS_TOOLS` | `false` | shell、文件、HTTP 等危险工具总开关 |
| `AGENTFORGE_FILE_ALLOWED_DIRS` | `[]` | JSON 数组；文件工具目录白名单 |
| `AGENTFORGE_HTTP_ALLOWED_HOSTS` | `[]` | JSON 数组；HTTP 主机白名单 |
| `AGENTFORGE_SANDBOX_ENABLED` | `false` | 启用 Docker 沙箱；不可用时非强制模式可回退本地沙箱 |
| `AGENTFORGE_SANDBOX_REQUIRED` | `false` | 强制 Docker；失败时拒绝执行，Compose 默认 `true` |
| `AGENTFORGE_SANDBOX_NETWORK_DISABLED` | `true` | 沙箱默认断网 |
| `AGENTFORGE_RUN_MIGRATIONS` | `true` | 本地启动迁移；Compose API 关闭，由 `migrate` 独占执行 |

真实中文向量需安装 `backend/requirements-embedding.txt` 并准备本地模型。hashing 支持中文检索和离线测试，但不是语义模型。沙箱强制模式使用 UID 10001、只读根文件系统、受限 tmpfs、`cap_drop=ALL`、no-new-privileges、PID/CPU/内存限制和默认断网；挂载 Docker socket 本身具有高权限，必须限制宿主访问。

## 核心 API

业务路由同时挂在 `/api/v1` 和 `/api` 下。以下省略该前缀，完整请求/响应 schema 以 Swagger 为准。

| 资源 | 主要端点 |
| --- | --- |
| Auth | `POST /auth/register`、`POST /auth/login`、`GET /auth/me` |
| 用户管理 | `GET /admin/users`、`PATCH /admin/users/{user_id}/role`（admin） |
| Agents | `GET/POST /agents`、`GET/PATCH/DELETE /agents/{agent_id}` |
| 导入导出 | `POST /agents/import`、`POST /agents/export`，支持 JSON/YAML |
| Runs | `GET/POST /agents/{agent_id}/runs`、`GET/DELETE .../runs/{run_id}` |
| Run 控制 | `POST .../runs/{run_id}/pause`、`/resume`、`/evaluate` |
| Evals | `GET/POST /agents/{agent_id}/evals`、`GET/DELETE .../evals/{eval_id}` |
| Eval 控制 | `GET .../evals/{eval_id}/progress`、`POST .../cancel`、`POST /agents/{agent_id}/evals/upload` |
| Memory | `GET/POST/DELETE /agents/{agent_id}/memory`、`DELETE .../memory/{memory_id}` |
| Tools | `GET /tools`、`GET /tools/{name}`；`PUT/DELETE /tools/{name}/metadata`（写操作 admin） |
| Prompt | `GET/POST /agents/{agent_id}/prompt-templates`、版本创建、`bind`、`compare` |
| 健康与指标 | `/health/live`、`/health/ready`、`/api/metrics`（admin，单实例数据） |

Memory 查询支持 `q`、`top_k` 和 `mode=exact|semantic|hybrid`。Run 支持 `async_run`；Eval 提交后通过 progress 轮询。WebSocket 路径为 `/agents/{agent_id}/runs/{run_id}/ws`，浏览器通过子协议传递凭据，避免把长期 token 放进 URL。

## 内置工具与 Scorer

十个内置工具：`echo`、`now`、`calculator`、`python_eval`、`http_request`、`shell_exec`、`file_read`、`file_write`、`search_knowledge`、`final_answer`。

- `python_eval` 支持 `simple` / `math` / `sandbox`，带 AST 限制；sandbox 模式在启用沙箱时走隔离执行。
- `http_request`、文件工具和 `shell_exec` 受危险工具开关与白名单约束。
- 工具元数据可在数据库中覆盖描述、风险、分类、参数 schema 和 enabled；禁用后不会进入 Agent 可用工具列表。
- 可通过 `@tool()` 注册自定义工具：

```python
from app.agent.tools import tool

@tool(description="Return a greeting for a city")
def greet_city(city: str) -> str:
    return f"Hello, {city}!"
```

评测支持结构化 scorer：`default` / `composite`、`status`、`tool`、`answer`，以及函数指标：`exact_match`、`contains`、`rouge_l`、`token_f1`、`numeric`、`json_match`。最终通过条件由请求的 `pass_threshold` 统一判定。

## 开发与验证

```bash
# 后端测试
cd backend
pytest -q

# 前端检查
cd ../frontend
npm run typecheck
npm run build
```

本次阶段 0～5 收尾验证：

- 后端全量分组回归：**323 项通过**。
- Alembic `0001`～`0007` 升级、降级与迁移链测试通过。
- 前端 TypeScript 检查和生产构建通过。
- Python 脚本编译、k6 脚本语法检查通过。
- SQLite 备份/恢复 roundtrip 通过；PostgreSQL `pg_dump/pg_restore` 尚未实际演练。

运维脚本：

```bash
# 升级前备份；先按生产手册停止写入
python scripts/backup.py --database-url "$AGENTFORGE_DATABASE_URL"

# 恢复会覆盖目标数据，必须显式确认
python scripts/restore.py --input backups/<BACKUP> \
  --database-url "$AGENTFORGE_DATABASE_URL" --yes

# 有界压测与大数据评测（需要服务已启动）
LOAD_SHAPE=short locust -f scripts/perf/locustfile.py \
  --host http://localhost:8000 --headless
python scripts/perf/large_eval.py --count 100 --concurrency 4
```

## 文档

- [生产部署与运维手册](docs/PRODUCTION.md)：启动、用户隔离、沙箱、备份、回滚、日志指标和上线清单。
- [性能验证计划](docs/PERFORMANCE.md)：short / ramp / spike / soak 与大数据评测。
- [项目介绍](docs/项目介绍.md)与[项目实际是什么](docs/这个项目实际是什么.md)。
- [零基础入门](docs/从零读懂%20agentforge（含操作步骤）.md)。
- [Mock 演示](docs/demo.md)。
- [CHANGELOG](CHANGELOG.md)：历史版本与本次改造摘要。

## 尚需目标环境验收

- Docker Compose 全栈实启、卷保留重启，以及 PostgreSQL / Redis / Qdrant 实际连通。
- 真实 Docker 沙箱的断网、只读文件系统、资源限制和安全复核。
- 本地中文 embedding 模型加载与检索质量。
- 真实 Provider 的模型名、API key、配额和连通性。
- PostgreSQL 备份恢复、迁移回滚演练。
- 压测与大数据评测的 p95、错误率、队列、CPU/RSS 等实测结果。

Celery 恢复采用 at-least-once 语义，运维时需检查任务重放；`/api/metrics` 是单实例指标，多实例必须分别采集。不要仅凭单元测试通过就把系统标记为“生产验收完成”。

## 贡献与许可

欢迎提交 Issue 和 PR。修改执行逻辑时请补充能证明行为的测试，并运行后端回归、前端类型检查与生产构建。

License: [MIT](LICENSE)。

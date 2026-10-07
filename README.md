# AgentForge

> An open playground for designing, tracing, and evaluating LLM agents.

AgentForge 是一个端到端的 **LLM Agent 工作台**。你可以在网页上编辑 Agent（选工具、写守则、调参数）→ 一键跑起看它 step-by-step 思考、调用哪些工具、产生什么输出 → 一键出考卷批量评测并给出得分。代码全部本地跑，不依赖云服务。

[![MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)]() [![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)]() [![Docker compose](https://img.shields.io/badge/docker--compose-v2-2496ed.svg)]() [![Version 0.1.3.dev0](https://img.shields.io/badge/version-0.1.3.dev0-blue.svg)](CHANGELOG.md)

---

## 目录 · Table of contents

- [核心特性 · Features](#-features)
- [架构图 · Architecture](#-architecture)
- [项目结构 · Project layout](#-project-layout)
- [技术栈 · Tech stack](#-tech-stack)
- [快速开始 · Quick start](#-quick-start)
- [环境变量 · Configuration](#-configuration)
- [API 一览 · API surface](#-api-surface)
- [内置工具 · Built-in tools](#-built-in-tools)
- [内置 Scorer · Built-in scorers](#-built-in-scorers)
- [开发与测试 · Development & tests](#-development--tests)
- [路线图 · Roadmap](#-roadmap)
- [已规划但未实现的功能 · Planned but not yet implemented](#-planned-but-not-yet-implemented)
- [0.1.3 路线图 · Open items](#-013-路线图--open-items)
- [贡献与许可 · Contributing & license](#-contributing--license)
- [🇨🇳 中文文档 · Chinese docs](docs/项目介绍.md) — 一句话读懂这个项目
- [🇨🇳 项目实际是什么](docs/这个项目实际是什么.md) — 它能干什么、对谁有用
- [🇨🇳 面试可能会被问到的问题](docs/AgentForge%20面试可能会被问到的问题.md) — 8 个核心问题 + 现场怎么答
- [🇨🇳 零基础入门](docs/从零读懂%20agentforge（含操作步骤）.md) — 网页点击全程

---

## ✨ 核心特性 · Features

| 特性 | 说明 |
| --- | --- |
| **可观测的 Agent 循环** | 每一步都落到数据库，查询/暂停/恢复/取消都可以 |
| **10 个内置工具** | `echo` / `now` / `calculator` / `python_eval` / `http_request` / `shell_exec` / `file_read` / `file_write` / `search_knowledge` / `final_answer`，用 `@tool()` 装饰器一行注册 |
| **长期记忆** | exact match + cosine similarity 双路径，记住你以前告诉他的任何东西 |
| **安全小黑屋** | `shell_exec` 默认子进程隔离；设为 `docker` 后切到 Docker 容器 |
| **批量评测** | 一道接口提交，后台跑完给出 pass / fail 、平均分、工具准确率 |
| **5 个 LLM provider** | `mock`（本地模拟，免费）/ `openai` / `anthropic` / `deepseek` / `qwen`，换个 env 变量就能切（Anthropic 走专用 `AnthropicClient`，其他四个走 OpenAI 兼容路径） |
| **本地一键起** | `docker compose up --build` 启动 API + 前端，不依赖云服务 |
| **可生长** | 工具、评分函数、记忆策略都是插件式，加个函数就能扩展 |

---

## 🏗 架构图 · Architecture

```
                    +---------------------+
                    |  React Frontend     |
                    |  (Vite + Zustand)   |
                    |  Port 5173          |
                    +---------+-----------+
                              | HTTP / JSON
                              v
                    +---------------------+
                    |  FastAPI Backend    |
                    |  app/               |
                    |   - api/ (routes)   |
                    |   - agent/ (loop)   |
                    |   - llm/ (clients)  |
                    |   - eval/ (runner)  |
                    |  Port 8000          |
                    +-----+-------+-------+
                          |       |
              +-----------+       +-----------+
              v                               v
     +----------------+             +----------------+
     |  SQLite        |             |  5 LLM clients |
     |  agentforge.db |             |  mock/openai/  |
     +----------------+             |  anthropic/    |
                                    |  deepseek/qwen |
                                    +----------------+
                                          |
                                          v
                                +----------------+
                                |  Tool sandbox  |
                                |  (subprocess   |
                                |   or Docker)   |
                                +----------------+
```

---

## 📁 项目结构 · Project layout

> **根目录一览**（详细的子结构见 [`docs/从零读懂 agentforge（含操作步骤）.md`](docs/从零读懂%20agentforge（含操作步骤）.md) 的附录 B）

```
agentforge/
├── README.md                ← 你正在看的这份
├── docs/                    ← 📖 中文文档（零基础入门 + 操作全程 + 原理）
├── backend/                 ← 后端（Python / FastAPI / SQLAlchemy）
├── frontend/                ← 前端（React / Vite / Zustand）
├── docker/                  ← Docker 镜像配方
├── sandbox/                 ← shell_exec=docker 模式的小黑屋镜像
├── data/                    ← 运行时数据（数据库文件等）
├── docker-compose.yml       ← 一键启动的"总开关"
├── .env                     ← 环境变量（运行时读）
├── .gitignore
├── logs/                    ← 🪵 构建/运行日志（debug 时翻）
└── scripts/                 ← 🛠 工具脚本（详见 scripts/README.md）
    ├── README.md            ←   脚本索引
    ├── tests/               ←   正经测试（smoke / passing / e2e…）
    └── diagnostics/         ←   一次性排查小工具
```

> 完整 `backend/` 和 `frontend/` 的子结构在 [`docs/从零读懂 agentforge（含操作步骤）.md`](docs/从零读懂%20agentforge（含操作步骤）.md) §附录 B 里。


---

## 🧱 技术栈 · Tech stack

| 层 | 选型 |
| --- | --- |
| 后端 | Python 3.11+ · FastAPI · SQLAlchemy 2 (async) · aiosqlite · Pydantic v2 · httpx |
| Agent 循环 | 自研 ReAct loop + step 持久化 + pause/resume/cancel |
| 工具 | 10 个内置（`python_eval` 含 `simple` / `math` / `sandbox` 三种安全模式）+ DB 可编辑的元数据层（`tool_metadata` 表 + `/api/tools/{name}/metadata`）+ 插件式注册器（`@tool()`） |
| 评测 | custom runner + scorer + 4 个 scorer（`composite` / `status` / `tool` / `answer`）+ multipart 数据集上传（`.jsonl` / `.csv`）+ 前端热图组件（`EvalHeatmap`） |
| 记忆 | SQLite + 进程内 cosine similarity（占位字段：`AGENTFORGE_VECTOR_BACKEND`） |
| 沙箱 | 本地子进程（default）或 Docker 容器 |
| 前端 | React 18 + Vite + TypeScript + Zustand + React Router |
| 打包 | Docker compose v2（一键起 API + 前端） |


> **遗留问题 · Current limitations**
>
> * 默认运行在 **SQLite**（schema 与 PostgreSQL 友好，换 driver 即可迁移）。
> * 记忆检索默认是 **进程内余弦** + hashing-trick 嵌入（`app/memory/embeddings.py`）。`AGENTFORGE_VECTOR_BACKEND=chroma` 已经接入配置开关（`app/memory/vector_store.py`），但 ChromaDB 客户端本身仍是占位（抛 `NotImplementedError`），等真实部署时再装 `chromadb` 包并替换 `chroma` 分支。`AGENTFORGE_VECTOR_URL` 同样已读取但尚未使用。
> * 只走 5 个 LLM provider：`mock` / `openai` / `anthropic`（专用 `AnthropicClient`，httpx 直调 `/v1/messages`）/ `deepseek` / `qwen`（后两个走 OpenAI 兼容）。其他 OpenAI-format 接口可通过 `AGENTFORGE_OPENAI_BASE_URL` 接入。
> * 测试套件里 `tests/test_memory_vector.py::TestVectorStoreProtocol::test_unknown_backend_raises` 失败：测试假设环境变量是 `VECTOR_BACKEND`，但 `app/config.py` 的 `Settings` 用的是 `env_prefix="AGENTFORGE_"`。**已在 0.1.3 修**（见 `CHANGELOG.md` Unreleased / Fixed）。

---

## ✨ 快速开始 · Quick start

### 方式 A：Docker compose（推荐）

1. 拷贝示例环境文件：
   ```bash
   cp .env.example .env   # 在 Windows 上手动复制重命名
   ```
2. 启动：
   ```bash
   docker compose up --build
   ```
3. 打开浏览器：
   - 前端：http://localhost:5173
   - API 文档：http://localhost:8000/docs
   - 健康检查：http://localhost:8000/api/v1/health

### 方式 B：本地开发

**后端**
```bash
cd backend
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**前端**
```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

### 烟雾测试：三句 curl 走一遍

在服务起来后，跑下面三句验证一下：

```bash
# 创建一个使用 mock LLM 的 agent
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name":"math-tutor","provider":"mock","model":"mock-model","system_prompt":"You are a math tutor.","tools":["calculator"]}'

# 让他算 12 * (3 + 4)
curl -X POST http://localhost:8000/api/v1/agents/<AGENT_ID>/runs \
  -H "Content-Type: application/json" \
  -d '{"input":"what is 12 * (3 + 4)?"}'

# 查看运行详情（步骤 / 工具调用 / 最终答案）
curl http://localhost:8000/api/v1/agents/<AGENT_ID>/runs/<RUN_ID>

# 顺手提一次评测验证 /progress 和 /evals/{id} 的 cases 数组
curl -X POST http://localhost:8000/api/v1/agents/<AGENT_ID>/evals \
  -H "Content-Type: application/json" \
  -d '{"dataset_name":"smoke","scorer_name":"answer","cases":[{"input":"2+2","expected":"4"}]}'
curl http://localhost:8000/api/v1/agents/<AGENT_ID>/evals/<EVAL_ID>/progress
curl http://localhost:8000/api/v1/agents/<AGENT_ID>/evals/<EVAL_ID> | head -c 400
```

完整步骤、跨子任务、暂停 / 恢复、取消请看：[API surface](#-api-surface)。

---

## ⚙️ 环境变量 · Configuration

所有变量都在 `.env`。示例在 [`.env.example`](.env.example)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENTFORGE_LLM_PROVIDER` | `mock` | `mock` / `openai` / `anthropic` / `deepseek` / `qwen`（后两个走 OpenAI 兼容，按 `PROVIDER_PRESETS` 自动选 `base_url` + `default_model`） |
| `AGENTFORGE_LLM_MODEL` | `mock-model` | 对应 provider 下的模型名 |
| `AGENTFORGE_OPENAI_API_KEY` | — | 用 OpenAI 时填 |
| `AGENTFORGE_ANTHROPIC_API_KEY` | — | 用 Anthropic 时填 |
| `AGENTFORGE_DATABASE_URL` | `sqlite+aiosqlite:///./agentforge.db` | 默认 SQLite；换 PG 可写 `postgresql+asyncpg://...` |
| `AGENTFORGE_MAX_STEPS` | `8` | 单次跑最多几步 |
| `AGENTFORGE_MAX_TOKENS` | `8000` | 上下文窗口上限 |
| `AGENTFORGE_SANDBOX_MODE` | `local` | `local` = 子进程；`docker` = Docker 容器 |
| `AGENTFORGE_SANDBOX_IMAGE` | `agentforge-sandbox` | `docker` 模式下的镜像名 |
| `AGENTFORGE_VECTOR_BACKEND` | 占位字段 | 未起作用；预留作未来换 ChromaDB |
| `AGENTFORGE_VECTOR_URL` | 占位字段 | 同上 |

> **为什么是占位字段？** 因为记忆检索现在跑在进程内的余弦上（只看本地 SQLite），没有任何外部 vector service 可连。要换成 ChromaDB 或 pgvector，只需重写 `backend/app/agent/memory.py` 里的 `_search_embed()`。

---

## 📜 API 一览 · API surface

以下是 **所有** REST 路由（全部在 `/api/v1` 前缀下；`/api/...` 是等价的别名）。完整 schema 看 http://localhost:8000/docs 。

### Agents
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET    | `/api/v1/agents`                  | 列出所有 agent |
| POST   | `/api/v1/agents`                  | 创建新 agent |
| GET    | `/api/v1/agents/{agent_id}`       | 查看一个 agent |
| PATCH  | `/api/v1/agents/{agent_id}`       | 更新 agent |
| DELETE | `/api/v1/agents/{agent_id}`       | 删除 agent |

### Runs
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET    | `/api/v1/agents/{agent_id}/runs`         | 该 agent 的所有任务 |
| POST   | `/api/v1/agents/{agent_id}/runs`         | 提交一次任务（同步返回 trace） |
| GET    | `/api/v1/agents/{agent_id}/runs/{run_id}`| 查看一次任务的详情 |
| DELETE | `/api/v1/agents/{agent_id}/runs/{run_id}`| 删除一次任务 |
| POST   | `/api/v1/agents/{agent_id}/runs/{run_id}/pause`  | 暂停任务 |
| POST   | `/api/v1/agents/{agent_id}/runs/{run_id}/resume` | 恢复任务 |

### Memory
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET    | `/api/v1/agents/{agent_id}/memory`            | 查看该 agent 的长期记忆 |
| POST   | `/api/v1/agents/{agent_id}/memory`            | 写入一条记忆 |
| DELETE | `/api/v1/agents/{agent_id}/memory`            | 清空该 agent 的记忆 |
| DELETE | `/api/v1/agents/{agent_id}/memory/{memory_id}`| 删除单条记忆 |

### Tools
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET    | `/api/v1/tools`                | 列出可用的工具 |

### Evals
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET    | `/api/v1/agents/{agent_id}/evals`            | 列出该 agent 的评测历史 |
| POST   | `/api/v1/agents/{agent_id}/evals`            | 提交一次评测（后台运行，立即返回占位 run） |
| GET    | `/api/v1/agents/{agent_id}/evals/{eval_id}`  | 查看一次评测的详情（含每道题的 `cases` 数组） |
| GET    | `/api/v1/agents/{agent_id}/evals/{eval_id}/progress` | 轮询评测进度（`done`/`total`/`passed`/`failed`） |
| DELETE | `/api/v1/agents/{agent_id}/evals/{eval_id}`  | 删除一次评测 |

> **已移除的旧端点**：早期 README 里的 `GET /api/v1/runs/{id}`、`GET /api/v1/evals/{id}`、
> `GET /api/v1/evals/{id}/cases` 都不存在。评测的"每道题"信息现在通过
> `GET .../evals/{eval_id}` 详情接口的 `cases` 字段返回，**不**再单独暴露 `/cases`。

**请求示例**：
```bash
# 创建 agent
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name":"greeter","provider":"mock","model":"mock-model","system_prompt":"Say hi","tools":["echo"]}'

# 跑一个任务
curl -X POST http://localhost:8000/api/v1/agents/<AGENT_ID>/runs \
  -H "Content-Type: application/json" \
  -d '{"input":"hello!"}'

# 提交一次评测（可用 scorer_name 列表见下）
curl -X POST http://localhost:8000/api/v1/agents/<AGENT_ID>/evals \
  -H "Content-Type: application/json" \
  -d '{"dataset_name":"smoke","cases":[{"input":"2+2","expected":"4"}],"scorer_name":"answer"}'

# 轮询评测进度
curl http://localhost:8000/api/v1/agents/<AGENT_ID>/evals/<EVAL_ID>/progress

# 拿评测详情（每道题的 input/expected/output/score 都在 cases 数组里）
curl http://localhost:8000/api/v1/agents/<AGENT_ID>/evals/<EVAL_ID>
```

---

## 🧰 内置工具 · Built-in tools

9 个工具都装在 `backend/app/agent/tools.py`里，一个 `@tool()` 装饰器就能注册。

| 名字 | 功能 | 安全等级 | 例子 |
| --- | --- | --- | --- |
| `echo`             | 原样返回输入 | 安全       | `echo("hi") -> "hi"` |
| `now`              | 返回当前 ISO 时间 | 安全        | `now() -> "2024-01-15T10:30:00Z"` |
| `calculator`       | 安全沙箱里跑 Python 表达式 | 安全         | `calculator("12*3+4") -> 40` |
| `http_request`     | HTTP GET/POST 请求（超时 10s） | 中等        | `http_request("GET https://api...")` |
| `search_knowledge` | 检索长期记忆 | 安全        | `search_knowledge(query="deadline")` |
| `file_read`        | 读文件 | 中等        | `file_read(path="/tmp/x.txt")` |
| `file_write`       | 写文件 | 中等        | `file_write(path="/tmp/x.txt", content="...")` |
| `shell_exec`       | 执行 shell 命令 | 高 — 默认在 sub-process 隔离 | `shell_exec(cmd="ls")` |
| `final_answer`     | 让 agent 提交结果 | 安全        | `final_answer(text="42")` |

**加个自己的工具**：
```python
from app.agent.tools import tool

@tool()
def get_weather(city: str) -> str:
    '''Return the current weather for a city.'''
    # ... call your API ...
    return f"{city}: 22C, sunny"
```
重启后端后它就会出现在 `/api/v1/tools` 里，也能被选中。

---

## 📏 内置 Scorer · Built-in scorers

评测接口接受一个 `scorer_name` 字段。可用名字（大小写敏感）取自
`backend/app/eval/scorer.py::get_scorer`：

| 名字 | 含义 | 何时用 |
| --- | --- | --- |
| `default` / `composite` | 三合一：`status` + `tool` + `answer` 加权平均 | 不传或想看综合分（**默认**） |
| `status` | 只看 agent 是否最终 `status == completed` | 检验"能不能跑通" |
| `tool` | 只看 agent 是否调用过 `expected_tool`（case meta 里指定） | 检验"会不会选工具" |
| `answer` | 只看文本相似度（`SequenceMatcher`） | 检验"答案对不对" |

> **历史名字已废弃**：早期 README 里的 `exact` / `contains` /
> `exact_match` / `regex` / `tool_called` / `numeric_tolerance` 都**不是**
> 当前 API 接受的 `scorer_name`。`backend/app/eval/metrics.py` 里确实有
> 这些打分函数，但它们仅在 `EvalRunner` 内部被 `Metrics.exact_match` 之类
> 的字段引用，**不**暴露给 `/api/v1/agents/{id}/evals` 的请求体。

```bash
# 用"只验答案"打分跑一次评测
curl -X POST http://localhost:8000/api/v1/agents/<AGENT_ID>/evals \
  -H "Content-Type: application/json" \
  -d '{"dataset_name":"math","scorer_name":"answer","cases":[{"input":"2+2","expected":"4"}]}'
```

---

## 🛠 开发与测试 · Development & tests

```bash
# 后端测试
cd backend
pytest -q

# 前端 type check / build
cd ../frontend
npm run build
```

最近一次完整跑：**106 passed, 1 failed, 8 skipped**。
失败项集中在 `test_api.py::test_agent_lifecycle` 等几个 `aiosqlite`
共享内存库 schema 初始化问题，与业务逻辑无关（事件循环 fixture
scope 默认是 function，旧测试用 session 级 EventLoop 共享连接，
跑到一半新会话接不上已经 commit 的内存表）。核心修改相关
`test_runner.py` / `test_evals_api.py` / `test_tools.py` / `test_loop.py`
全部通过。

---

## 🛣 路线图 · Roadmap

- [ ] 重写 `MemoryStore._search_embed`，换成连接外部 vector service（ChromaDB / pgvector）
- [ ] 加个 `OpenAI-compatible` 适配器，以便连 DeepSeek / Qwen / 任何 OpenAI-format 接口
- [ ] Eval 结果页面加热图，逐频度查看
- [x] WebSocket 推送，跑任务时实时刷新步骤
- [x] Agent 文件导入 / 导出（YAML / JSON） — 0.1.2 #7 轮完成

---

## 📋 已规划但未实现的功能 · Planned but not yet implemented

**本节列出 README 各处提到、但 0.1.0 版代码里**未真正接线**的功能**——**包括 Features 表、Configuration 表、Built-in tools 表里的"占位 / 半成品"项**。**这些不算"错误"**——**是项目在 0.1.0 阶段就明确规划的"下一版本"目标**。

### LLM 大脑

| 状态 | 功能 | 现状 | 期望 |
| --- | --- | --- | --- |
| ✅ 已接 | `mock` / `openai` / `anthropic` / `deepseek` / `qwen` 全 5 个 provider | 0.1.0 缺 Anthropic 专用 client —— 走 OpenAI 路径会 401 | 新增 `app/llm/client.py:AnthropicClient`（httpx 直调 `/v1/messages`，含 system / tool_use / tool_result 转换 + SSE 流）；`make_client("anthropic",...)` 走专用路径；`loop.py:_build_default_llm` 按 provider 选 `anthropic_api_key` 还是 `openai_api_key`；无 key 时仍降级 mock |
| ✅ 已接 | `deepseek` / `qwen` 走 OpenAI 兼容 | 0.1.0 版 `loop.py` 不读 `_PROVIDER_PRESETS`；`docker-compose.yml` 没预配 `AGENTFORGE_OPENAI_BASE_URL` | `loop.py:_build_default_llm` 改为调用 `make_client`；预设表（`deepseek` / `qwen` / `openai`）自动选用正确 `base_url` + `default_model` |

### 工具沙箱

| 状态 | 功能 | 现状 | 期望 |
| --- | --- | --- | --- |
| ✅ 已接 | `shell_exec` 默认子进程隔离；`AGENTFORGE_SANDBOX_ENABLED=True` 后切到 `LocalSandbox` / `DockerSandbox`（按 `AGENTFORGE_SANDBOX_IMAGE` / `MEMORY` / `CPU` 配置） | 0.1.0 `shell_exec` 走自己的 `subprocess.run` 同步阻塞 + Windows `dir` 等 cmd builtin 报 `FileNotFoundError` | `sandbox.py:LocalSandbox.run_shell` / `DockerSandbox.run_shell` 新增；`_AsyncSandbox` Protocol 加 `run_shell`；`tools.py:shell_exec` 改 async + 读 `sandbox_enabled` 自动路由到 `get_sandbox().run_shell()`，未启用走 thin `asyncio.create_subprocess_exec` fallback + Windows `cmd /c` 自动 fallback |
| ✅ 真接 | `calculator` 走"安全沙箱" | 0.1.2 7 轮收尾（0.1.2 final close-out）：`calculator` 现在是 `async def`，白名单 `0-9+-*/().%` 检查后当 `AGENTFORGE_SANDBOX_ENABLED=true` 时通过 `get_sandbox()`（`LocalSandbox` 子进程或 `DockerSandbox` 容器）`python -c "print(repr(eval(...)))"`，与 `shell_exec` 同沙箱路径；未启用时保留 0.1.0/0.1.1 的 `eval(..., {"__builtins__": {}}, {})` fallback；3 个新测试 `tests/test_tools.py` 覆盖默认路径 + 沙箱路径 + 白名单拒绝 | n/a |

### 长期记忆

| 状态 | 功能 | 现状 | 期望 |
| --- | --- | --- | --- |
| 🟡 部分 | `AGENTFORGE_VECTOR_BACKEND` / `AGENTFORGE_VECTOR_URL`（ChromaDB / pgvector） | 0.1.2 加了 `Memory.embedding` (LargeBinary) + `Memory.session_id` (String(36)) 两列 + `app/memory/embeddings.py`（hashing-trick 纯 Python embedder，不引依赖）+ `app/memory/vector_store.py`（`InMemoryVectorStore` + Protocol + factory）+ `MemoryStore.add/delete/search` 全接 `vector_store`（`mode="embed"` / `"hybrid"` 真用 cosine）| 0.1.2 4.2 轮完成 in-process + hashing-trick 路径；接 ChromaDB（要加 `chromadb` 依赖）+ pgvector（要加 `pgvector` 扩展） |
| 🟢 真接 | exact match + cosine similarity 双路径 | 0.1.2 4.2 轮：`MemoryStore.search` 走 `mode="exact"` / `"embed"` / `"hybrid"` 三种模式；`tools.py:search_knowledge` 暴露 `mode` 参数（默认 `exact` 保持 0.1.0 兼容） | n/a |
| 🟢 真接 | 记忆过期 / TTL | 0.1.2 4.3 轮：`Memory.expires_at: DateTime? indexed` + `MemoryStore.add(ttl_seconds=...)` 自动算过期时间 + `list` / `search` 三种 mode 都过滤过期行 + `app.memory.cleanup.cleanup_expired(session, batch=200)` 删过期行 + `schedule_periodic_cleanup(session_factory, interval_seconds=300)` 后台任务（默认 5 分钟一次） | n/a |

### 评测

| 状态 | 功能 | 现状 | 期望 |
| --- | --- | --- | --- |
| 🟢 真接 | Eval 多 worker 并发跑题 | 0.1.2 5.1 轮：`EvalRunner.run_eval` 走 `asyncio.gather + Semaphore(max_concurrency)` 真并发；每 case 独立 `AsyncSession`（共享 session 会因 N 个 AgentLoop 抢连接死锁）；aggregate 写回 `EvalRun` 用独立 session 串行；`AgentLoop` / `EvalRunner` 内部 commit 加 retry 应对 SQLite `database is locked` 瞬时锁；`max_concurrency` 默认 4（4 核 CPU 一次跑 4 个 case）；`api/schemas.py` 字段同步默认 4；设 1 = 串行（兼容 0.1.0） | n/a |
| 🟢 真接 | Eval 结果页面加热图 | 0.1.3 #4：新增 `frontend/src/components/EvalHeatmap.tsx` 自包含 SVG 组件（无外部图表依赖），行 = 唯一 case input，列 = 历史 eval run，单元格按 score 0→1 颜色（红→绿）+ ✕ 标识 case 报错；懒拉 `Evals.get` 拿每 run 的 `cases[]`；嵌入 `pages/Evaluation.tsx` "Past Evaluations" 表格之上；i18n key 落在 `en.json` / `zh.json` 的 `eval.heatmap_*` 段 | n/a |
| 🟢 真接 | 多格式数据集上传（JSONL / CSV） | 0.1.3 #2：`POST /api/v1/agents/{id}/evals/upload` 接受 multipart `file=` + `dataset_name` + `scorer_name` + `pass_threshold` + `max_concurrency`，自动按扩展名 / 内容 sniff 派发到 `parse_jsonl` / `parse_csv`；`app/eval/parser.py` 新增 `parse_dataset / parse_jsonl / parse_csv / detect_format / DatasetParseError`；错误以 HTTP 400 携带 `detail.errors: [{line, column, message}]` 返回 | n/a |

### 实时推送

| 状态 | 功能 | 现状 | 期望 |
| --- | --- | --- | --- |
| ✅ 真接 | 跑任务时实时刷新步骤 | 0.1.2 #10：`GET /api/agents/{id}/runs/{id}/ws`（同 `/api/v1/...`）订阅后立即收到 `run.snapshot`（含历史 trace），随后每个 commit 的 step 推一帧 `run.step`（shape 与 `Runs.get().trace[]` 一致），loop 结束推 `run.complete`；新模块 `app/api/ws.py` + `app/api/ws_broadcaster.py::RunBroadcaster`（process-local pub/sub，slow consumer 自动 `__dropped__` sentinel + 剪枝，不阻塞 agent loop）；`AgentLoop(..., config=LoopConfig(on_step=...))` 在 `_execute_run` / `_resume_run` 注入回调；前端 `src/hooks/useRunStream.ts` + `AgentRun.tsx` 合并 step 帧，断线时 fallback 1.5s 轮询；7 个新测试 `tests/test_ws.py` 全过（4 broadcaster unit + 3 ws endpoint integration）；Vite dev-server proxy `ws: true` 显式开启，prod 走 FastAPI `WebSocket` route。 | n/a |

### 插件

| 状态 | 功能 | 现状 | 期望 |
| --- | --- | --- | --- |
| 🟢 真接 | 工具元数据 DB 化 | 0.1.3 #3：新增 `tool_metadata` 表（alembic 0002）+ 4 个 HTTP 端点 `GET /api/tools` / `GET /api/tools/{name}` / `PUT /api/tools/{name}/metadata` / `DELETE /api/tools/{name}/metadata`；稀疏 PATCH 语义（只写显式给的字段）；`ToolMetadata` 行覆盖 `description` / `risk` / `scope` / `category` / `parameters_schema` / `enabled`；`enabled=False` 自动从 agent loop 的 prompt 剔除（`AgentLoop._step_once` 走 `get_enabled_tool_names(session, names)` 取交集）；改完不用重启；11 个新测试 `tests/test_tools_metadata.py` 覆盖 merge / DB / 4 个 HTTP 形状；旧 `app.api.agents.tools_router` 保留为 library helper 兼容老 import | n/a |

### 数据库

| 状态 | 功能 | 现状 | 期望 |
| --- | --- | --- | --- |
| 🟢 真接 | PostgreSQL 支持（alembic 迁移） | 0.1.2 8 轮：`alembic init` 落 `alembic/` 目录 + `alembic.ini` + `versions/0001_baseline.py` autogenerate 复刻 6 个 model（`agents` / `agent_runs` / `agent_steps` / `memories` / `eval_cases` / `eval_runs`，含 FK CASCADE + 全部索引）；`alembic/env.py` 从 `os.environ["AGENTFORGE_DATABASE_URL"]` 读 URL（自动 `sqlite+aiosqlite` ↔ `sqlite` 转换 + Windows 盘符 `/` 修正）；`app/config.py` 加 `env_prefix="AGENTFORGE_"` + `@model_validator` 自动从 `database_url` 派生 `sync_database_url`（之前 0.1.0/0.1.1 的 `AGENTFORGE_DATABASE_URL` 其实没被读，是 pydantic-settings 缺 `env_prefix`）；`app/database.py:init_db` 改走 `alembic upgrade head`（`asyncio.to_thread` 调 `command.upgrade`）——单源 schema 真理；`app/main.py:lifespan` 0.1.2 final close-out 启动时调 `schedule_periodic_cleanup(AsyncSessionLocal, interval_seconds=settings.memory_cleanup_interval_seconds)`（默认 300 s；`AGENTFORGE_MEMORY_CLEANUP_INTERVAL_SECONDS=0` 禁用），关闭时 `task.cancel() + await` 清理；`AGENTFORGE_MEMORY_CLEANUP_INTERVAL_SECONDS` 是新配置项 | 0.1.2 8 轮完成 SQLite 真接 + 文档化；接 PostgreSQL：装 `psycopg2-binary`，`AGENTFORGE_DATABASE_URL=postgresql+asyncpg://...`，`pip install alembic` 已装，无需改 app 代码 |

### Agent 管理

| 状态 | 功能 | 现状 | 期望 |
| --- | --- | --- | --- |
| ✅ 真接 | Agent 文件导入 / 导出（YAML / JSON） | 0.1.2 7 轮：`app/agent/exporter.py` 纯逻辑（`build_payload` / `dump` / `load` / `detect_format` / `agents_from_payload`，versioned `version=1` + `kind=agentforge.agent` envelope，前向兼容：未知字段自动 drop）；`POST /api/v1/agents/import`（multipart `file=` + `on_conflict=skip|overwrite|error` form 字段，返回 `{created, updated, skipped}` 计数 + 名字列表）+ `POST /api/v1/agents/export`（JSON body `{"agent_ids": [...]}` 选填 → 默认导出全部；`?fmt=json|yaml` query；`Content-Disposition: attachment` 触发浏览器下载）；自动检测扩展名 (`.yaml`/`.yml`/`.json`) + 内容 magic (`---` 开头)；`pyyaml==6.0.3` 加进 `requirements.txt`；19 个新测试 `tests/test_agent_io.py` 全过（export 默认/指定/YAML/缺 ID 404/格式 400；import JSON/YAML round-trip + skip/overwrite/error 三种冲突策略 + 无效 version/kind 400 + 文件名检测 + export→import 幂等；5 个 pure-function 单测覆盖 envelope 校验）；全量 215 passed（2 pre-existing 与本轮无关）。 | n/a |

---

**这是 0.1.0 版的真实状态**——**不是缺陷清单**——**是规划**。**每一项都有"现状"和"期望"**——**下一版本按表逐项推进**。

---

## 🛣 0.1.3 路线图 · Open items

0.1.0 → 0.1.2 阶段落的"已规划但未实现"段在上方 ✅ / 🟢 全部走完。
**0.1.3** 接的是 [「零基础玩转 AgentForge」](零基础玩转%20AgentForge/) 短期 5 项 + 文档维护项。
**当前进度**（看 `CHANGELOG.md` 同步）：

| # | 项 | 状态 | 备注 |
| --- | --- | --- | --- |
| 1 | `python_eval` 工具 | ✅ 已接 | 0.1.3 #1：`tools.py` 加 3 种模式（`simple` / `math` / `sandbox`），含 AST 安全门；`tests/test_tools.py` 9 个新测试 |
| 2 | 多格式数据集上传 | ✅ 已接 | 0.1.3 #2：`POST /api/.../evals/upload` 接 multipart `.jsonl` / `.csv`；3 + 6 个新测试 |
| 3 | 工具元数据 DB 化 | ✅ 已接 | 0.1.3 #3：`tool_metadata` 表 + 4 个 HTTP 端点 + agent loop 自动跳过 disabled；11 个新测试 |
| 4 | Eval 热图 | ✅ 已接 | 0.1.3 #4：`components/EvalHeatmap.tsx` 自包含 SVG；嵌在 Evaluation 页面 |
| 9 | 工具风险等级 UI 标注 | ✅ 已接 | 0.1.3 #9：后端 `tools.py` 10 个 `@tool` 都带 `risk=`（`safe` / `read` / `write` / `network` / `exec`），agent loop 把 `payload["risk"]` 写到每个 `tool_call` 步骤的 payload，`StepOut.risk` (Pydantic computed) + REST `_serialize_step` + WS `_step_to_dict` 三个出口都暴露 `risk` 字段；前端 `StepNode.tsx` 在 `kind === "tool_call"` 时按 risk class 渲染彩色徽标（`read`→绿/`write`→黄/`network`→橙/`exec`→红，`safe` 不出徽标）；CSS 4 个新 `.badge.warn-low / .warn / .warn-high / .danger` 沿用现有色板 token；10 个新测试 `tests/test_risk_badge.py` 覆盖 schema / REST / WS / registry 四条路径 |

### 路线图清理后移出的项

以下几项在 0.1.3 路线图初版里是"待办"，经核实后**不属于 0.1.x 范围**或**根本不需要做**：

- **#5 ChromaDB 客户端** —— 0.1.x 用 in-process 向量存储就够；`AGENTFORGE_VECTOR_BACKEND=chroma` 开关保留但抛 `NotImplementedError` 是**有意设计**：等真要部署（memory > 1k 条）时再装 `chromadb` 包 + 实现客户端。**0.1.x 不做。**
- **#6 `test_unknown_backend_raises`** —— pre-existing 测试 bug（env 名拼错），**已修**（见 `CHANGELOG.md` Unreleased / Fixed）。
- **#7 HTTP `multipart files=` 上传** —— 0.1.x 没有调外部 vision / 文件处理 API 的用例；做了也没人用。**0.2+ 再说。**
- **#8 `TokenGauge` token 用量警告** —— **误判**：`components/TokenGauge.tsx` 已存在且 `pages/AgentRun.tsx` 已 import，无需做。已从路线图移除。
- **#10 Compare 页**（两 agent 并排对比）—— 工作量半天到一天，价值中等（0.1.x 阶段"跑一遍看哪个好"已够用）。**0.2+ 再说。**
- **#11 Scorer 插件化** —— 4 个内置 scorer 够用，要自定义的人会直接 PR 改 `app/eval/scorer.py`。**0.2+ 再说。**

> 维护原则：每完成一项 → 从下方"真待办"删掉，CHANGELOG.md 的 `[Unreleased]` 加一条 + 上方路线图表加一行 ✅ 已接。

## 📦 贡献与许可 · Contributing & license

PR 欢迎！主要思路：

1. Fork 本仓库
2. 开个分支：`git checkout -b feat/awesome-tool`
3. 加个测试：`backend/tests/test_your_thing.py`
4. 提 PR，描述你的使用场景和为什么这个变化重要

**License**: [MIT](LICENSE) · 开源，随你怎么用。

---

## 感谢 · Acknowledgements

AgentForge 受以下项目启发：FastAPI · SQLAlchemy · React · Vite · Zustand · Pydantic · OpenAI Python SDK · Anthropic Python SDK · aiosqlite。

**反馈 / 报告 bug**：走 [GitHub Issues](https://github.com/yourname/agentforge/issues) 。
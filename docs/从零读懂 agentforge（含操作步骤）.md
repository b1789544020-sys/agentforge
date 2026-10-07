# 从零读懂 agentforge（含操作步骤）

> **这份文档想做一件事：让一个完全没接触过 agentforge 的人，从"这是什么"到"怎么点鼠标"再到"出问题怎么办"，一份文档全讲明白。**
>
> 读之前**不需要懂大模型、不需要懂 FastAPI、也不需要懂 SQLAlchemy**——每个新概念第一次出现时，都会先打个比方让你脑子里有画面，然后再落到代码，最后再讲**在网页上到底该点哪里**。
>
> 主比喻是**小餐厅**：
> - **你** = 餐厅老板
> - **AI 模型** = 服务员（脑子灵，但只会说不会做）
> - **工具** = 服务员腰包里的物件（计算器、闹钟、文件柜……）
> - **记忆** = 服务员的小本本
> - **前端** = 餐厅前台
> - **后端** = 餐厅后厨总控
> - **数据库** = 收银台的小抽屉
> - **评测** = 老板的考核本
>
> **这份文档 = 老板的"经营理念手册 + 入职培训手册"二合一**。左边讲"为什么要这样设计"，右边讲"具体点哪个按钮"。

---

## 目录

> 每章结构都是：**先讲原理 → 再讲代码在哪 → 最后讲网页上怎么点**。
> 想跳着看也行，每章都标了 `[对应操作章节 §X.Y]` 互相指路。

- [0. 在你开始之前：AgentForge 到底是干嘛的](#0-在你开始之前agentforge-到底是干嘛的)
- [1. 把项目想象成一家小店](#1-把项目想象成一家小店)
- [2. 大文件夹里都有哪些小工具](#2-大文件夹里都有哪些小工具)
- [3. 钻进后厨：backend/app 里都有什么](#3-钻进后厨backendapp-里都有什么)
- [4. 服务员的工作循环 loop.py](#4-服务员的工作循环-looppy)
- [5. 工具腰包 tools.py](#5-工具腰包-toolspy)
- [6. 服务员的小本本 memory.py](#6-服务员的小本本-memorypy)
- [7. 评测系统：出题、收卷、批改](#7-评测系统出题收卷批改)
- [8. 一键端到端跑一遍（网页全程）](#8-一键端到端跑一遍网页全程)
- [9. 加一个新工具（开发入门）](#9-加一个新工具开发入门)
- [10. 总结 + 路线图](#10-总结--路线图)
- [附录 A. 深度体验 + 出错怎么办 + 概念速查](#附录-a-深度体验--出错怎么办--概念速查)
- [附录 B. 完整目录结构](#附录-b-完整目录结构你看到的文件夹里都有啥)
- [附录 C. 写给想贡献代码的人](#附录-c-写给想贡献代码的人)

---

## 0. 在你开始之前：AgentForge 到底是干嘛的

> **这一节是给"完全零基础"的人看的。** 你可以跳过——但如果你之前没听过 "agent"、"工具调用" 之类的词，建议先读这一段。

### 0.1 用一个生活比喻说清楚 AgentForge 是什么

> 想象你是一个**小餐厅老板**。你雇了一个**聪明的服务员**（这个服务员就是 AI 大语言模型，比如 ChatGPT），他什么都懂，但有一个致命问题——**他只会"说"，不会"做"**。
>
> 你跟他说"帮我算一下 12 乘以 (3 加 4) 是多少"，他可能会算错，因为数学对他来说就像你让一个老外用筷子夹花生米，他懂道理但手上没数。
>
> **AgentForge 就是给这个服务员配了一个"工具腰包"**。腰包里装着：
> - 计算器（数学问题他可以掏出计算器来算）
> - 闹钟（能告诉你现在几点）
> - 文件柜（能读文件、写文件）
> - 计算外壳（能在安全的小黑屋里跑程序）
> - 等等等等
>
> 有了这个腰包，这个服务员就**既会说又会做**了。你告诉他"用计算器算一下 12 乘以 (3 加 4)"，他就会掏出计算器，啪啪啪按几下，告诉你答案是 84——**保证不会错**。
>
> 那 AgentForge 还做了什么呢？它还：
> 1. **让你自己决定这个腰包里装哪些工具**（要计算器还是要文件柜，由你定）
> 2. **让你自己决定这个服务员的人设**（他是数学老师？还是翻译官？还是客服？）
> 3. **每次服务员干活的时候，把他说的话、做的动作、用的工具，全部用小本本记下来**（这样你可以回头检查他有没有偷懒、犯错）
> 4. **让他做一份试卷**（比如 10 道数学题），看看他能答对几道
> 5. **比较不同的服务员**（比如数学老师 vs 物理老师谁考得更好）
>
> 所以，**AgentForge = 一个给 AI 服务员配工具、布置任务、批改作业的"管理工作台"**。

### 0.2 它跟 ChatGPT 有什么区别？

| 工具 | 它能做什么 | 它不能做什么 |
| --- | --- | --- |
| ChatGPT 网页 | 跟你聊天、回答问题、写文章 | 不会真的去算数学、不会真的去读你电脑上的文件 |
| AgentForge | 你可以**自己设计**一个 AI 助手，让它**真的能动手做事**，并且**记录它每一步在干什么**，**批改它的作业** | 不能像 ChatGPT 那样直接用——它需要你先"启动"它 |

简单说：**ChatGPT 是你用别人的成品；AgentForge 是你自己当老板，组装一个专属的、能动手的 AI 员工。**

### 0.3 你将要做什么？

按照本手册，你将：

1. 把 AgentForge 这个"工作台"**安装到你电脑上**（就像你装一个 QQ 一样）—— 见 §8.0
2. **打开它的网页**（它长得像一个小网站）—— 见 §8.1
3. **雇一个 AI 服务员**（给它起名字、选工具）—— 见 §8.2
4. **给它派活**（输入一个问题，看它怎么干）—— 见 §8.4
5. **查它的作业本**（看它每一步都在干啥）—— 见 §8.5
6. **给他出考卷**（让它做 10 道题，看它能答对几道）—— 见 §8.6

**全部不需要写一行代码。** 高级玩法（写代码加新工具、跑批量测试）放在 §9 和附录 A。

---

## 1. 把项目想象成一家小店

打开 `agentforge/` 这个文件夹，第一眼看上去就是一堆文件和文件夹。
要是一上来就一个个点开看，肯定会懵。换个角度想：把它当成一家**小餐厅**就清楚多了。

| 角色 | 在 agentforge 里是什么 | 对应代码位置 |
| --- | --- | --- |
| 餐厅老板 | **你**（决定要招什么人、给什么人配什么工具） | — |
| 服务员 | **AI 模型**（脑子很灵，但只会说不会做） | `backend/app/llm/client.py` |
| 服务员的腰包 | **工具**（计算器、闹钟、文件柜……能动手干活的物件） | `backend/app/agent/tools.py` |
| 服务员的小本本 | **记忆**（短期的小本本 + 长期的小本本） | `backend/app/agent/memory.py` |
| 餐厅前台 | **前端**（Vite + React 写的网页，客人点单用的） | `frontend/src/` |
| 餐厅后厨总控 | **后端**（FastAPI 写的，接到单子后调度服务员干活） | `backend/app/main.py` |
| 收银台的小抽屉 | **数据库**（SQLite 文件，存所有客人点过什么单） | `agentforge.db` |
| 老板的考核本 | **评测系统**（给服务员出考卷、批改、算通过率） | `backend/app/eval/` |

这一整套搭配起来，就是 agentforge 这个项目的全貌。后面所有的小文件，都可以归到上面这张表里的某一栏。

---

## 2. 大文件夹里都有哪些小工具

整个 `agentforge/` 打开看，结构大致是这样（**这是新整理过的版本**，比早期版本清爽很多）：

```
agentforge/
├── README.md              # 项目对外的"产品说明书"（英文）
├── docs/                  # 📖 所有中文文档（你正在读的这份就在这）
├── backend/               # 后端（餐厅后厨总控）
├── frontend/              # 前端（餐厅前台）
├── docker/                # Docker 镜像配方（"菜谱"）
├── sandbox/               # 安全小黑屋的镜像
├── data/                  # 运行时数据（数据库文件等）
├── docker-compose.yml     # 一键起服务的"总开关"
├── .env                   # 环境变量（后端起动时读）
├── .gitignore             # 告诉 Git 哪些文件不要管
├── logs/                  # 🪵 各种构建/运行日志（debug 时翻）
└── scripts/               # 🛠 工具脚本（详见 scripts/README.md）
    ├── tests/             #   正经测试脚本
    └── diagnostics/       #   一次性排查工具
```

把这些文件挨个想象一下：

- **README.md** 是**贴在餐厅门口的菜单+价目表**——别人来参观的时候，看这一份就知道这家店卖什么。
- **docs/** 是**老板自己写的内部资料**——员工手册、理念说明全在这。**你正在读的这份文档就在 `docs/从零读懂 agentforge（含操作步骤）.md`**。
- **backend/** 是**后厨**——所有真正"做饭"的代码都在这。
- **frontend/** 是**前台**——客人点单时看到的小网站。
- **docker-compose.yml** 是**电闸箱**——推一下，整个店（后厨 + 前台 + 数据库）一起通电。
- **.env** 是**餐厅的内部小纸条**——写着"今天用 mock 大厨" / "最多接待 10 桌" / "令牌预算 8000" 这种内部配置。
- **logs/** 是**餐厅的监控录像**——平时不看，出事了调出来回放。
- **scripts/** 是**老板工具箱**——里头有正经测试脚本（`tests/`）和一次性排查小工具（`diagnostics/`）。**详细看 [`scripts/README.md`](../scripts/README.md)**。

下面就钻进 `backend/` 看看后厨里头是什么样。

---

## 3. 钻进后厨：backend/app 里都有什么

`backend/app/` 下面按"职责"分成几个小房间，每个房间里都有自己的工具。先看一眼地图：

```
backend/app/
├── main.py             # 大门
├── database.py         # 收银抽屉
├── models.py           # 菜单上的菜品的"模板"
├── config.py           # 读 .env 的小秘书
├── agent/              # 服务员的工作区
│   ├── loop.py         # 服务员的"工作循环"
│   ├── tools.py         # 服务员的"工具腰包"
│   ├── memory.py        # 服务员的"小本本"
│   ├── context.py       # 上下文窗口管理
│   └── sandbox.py       # 安全小黑屋
├── api/                # 前后台之间的"传菜窗口"
│   ├── agents.py
│   ├── runs.py
│   ├── evals.py
│   ├── memory.py
│   └── tools.py
├── eval/               # 老板的"考核办公室"
│   ├── runner.py        # 出题、收卷、批改
│   ├── scorer.py        # 改卷规则
│   └── metrics.py       # 改卷时用到的尺子和橡皮
├── llm/                # 大模型对接层（client.py）
└── schemas.py          # 前台点单时填的"小票格式"
```

挨个用日常的话翻译一下：

### 3.1 main.py — 大门

客人（前端、curl 脚本）进门的地方。装一个 FastAPI 框架，把所有"传菜窗口"（API 路由）挂上去。**不亲自做任何事**，只负责接单和派单。

应用启动时干两件事：
1. `init_db()` —— 让 SQLAlchemy 把所有表建好
2. 把 5 个 `api/*.py` 里定义的路由全注册到 `/api/v1/` 下面

### 3.2 database.py — 收银抽屉

管数据库连接的。这里用的是 SQLite + 异步 SQLAlchemy。**开张时（应用启动时）会自动把所有抽屉隔板（表）建好**，所以你不用手动建库。

```python
# 关键代码片段（简化）
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
# 代码默认:  sqlite+aiosqlite:///<BASE_DIR>/agentforge.db  (config.py:47)
# docker compose 实际:  sqlite:///./data/agentforge.db  (compose env 覆盖)
DATABASE_URL = "sqlite+aiosqlite:///./data/agentforge.db"
engine = create_async_engine(DATABASE_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
```

> **路径差异说明**：`backend/app/config.py:47` 的代码默认是 `<backend>/agentforge.db`，但 `docker-compose.yml:33` 通过环境变量 `AGENTFORGE_DATABASE_URL` 把它覆盖成 `./data/agentforge.db`（数据卷持久化）。所以**用 docker compose 启动后**，数据库文件落在项目根的 `data/agentforge.db`。

### 3.3 models.py — 菜品模板

数据库里每一张表（Agent、Run、EvalRun、EvalCase、Memory……）在这里都有一个对应的 Python 类。**类写好了，表就定了**。

每个模板长什么样：

| 模板 | 字段 | 比喻 |
| --- | --- | --- |
| **Agent** | name、provider、model、system_prompt、tools | 一个 AI 服务员的"档案卡" |
| **Run** | agent_id、input、status、steps、output | 一次"干活"的完整记录 |
| **Step** | run_id、index、kind、name、content、payload、duration_ms、tokens、status、error | 一次 Run 里的一小步（"想/调用/结果/收工"） |
| **EvalRun** | agent_id、dataset、scorer、pass_threshold、total/passed/failed | 一次"考核"的成绩单 |
| **EvalCase** | input、expected、output、score、passed、duration_ms、tokens、tool_calls、score_details、error | 考核卷子里的"一道题" |
| **Memory** | id、agent_id、content、kind、importance、meta、created_at | 服务员长期记住的事情（每条记忆 = 文本 `content` + 类别 `kind` + 重要度 `importance`） |

**对应操作**：[§8.2 建第一个 agent](#82-建第一个-agentconfiguration-tab)

### 3.4 agent/ — 服务员的工作区

```
agent/
├── loop.py         # 最有意思的一段——服务员循环
├── tools.py         # 9 个内置工具
├── memory.py        # 长时记忆
├── context.py       # 上下文窗口管理（防超出 max tokens）
└── sandbox.py       # shell_exec 的安全壳

> 注：不同 LLM 厂商的适配（mock / openai / anthropic）**没有单独的 providers/ 目录**——它们都集中在 ackend/app/llm/client.py 一个文件里。
```

详细讲解放到 §4、§5、§6 单独说。

### 3.5 api/ — 传菜窗口

| 文件 | 路由前缀 | 比喻 |
| --- | --- | --- |
| `agents.py` | `/api/v1/agents` | 员工管理（建/查/删/改） |
| `runs.py` | `/api/v1/agents/{id}/runs` | 派活（POST 新单 / GET 看记录） |
| `evals.py` | `/api/v1/agents/{id}/evals` | 考核（POST 考卷 / GET 进度 / GET 详情） |
| `memory.py` | `/api/v1/agents/{id}/memory` | 长时记忆（增/查/删） |
| `tools.py` | `/api/v1/tools` | 看腰包里有哪些工具（只读） |

完整路由清单可以直接看 `backend/openapi.json`（FastAPI 自动生成的）；**网页上能看到带表单的版本**就在 `http://localhost:8000/docs`（Swagger UI），详见 §A.2。



### 3.7 ChromaDB — 向量记忆后端

`docker-compose.yml:140-160` 还启了第 4 个服务：**ChromaDB**（`http://localhost:8001`）作为向量记忆后端。

- `AGENTFORGE_VECTOR_BACKEND=chromadb`（默认）→ 用 ChromaDB 存向量
- `AGENTFORGE_VECTOR_BACKEND=none` → 禁用向量记忆，`memory.search()` 只走 `MemoryStore._search_exact`（`memory.py:127`）

`.env` 里的 `AGENTFORGE_VECTOR_BACKEND` 决定走哪条路。**对零基础用户**：默认配置就行，不用管它——它跟 `MemoryStore` 配合，前端用不到。

### 3.6 eval/ — 老板的考核办公室

```
eval/
├── runner.py    # 出题 + 收卷 + 汇总
├── scorer.py    # get_scorer(name) — 改卷尺子
└── metrics.py   # 尺子的具体算法（exact_match、SequenceMatcher 等）
```

`scorer.py` 里有一个**注册表**，外面能用的 `scorer` 字段值就那么几个：

| `scorer`（API 字段名） | 含义 | 何时用 |
| --- | --- | --- |
| `default` / `composite` | 三合一：`status` + `tool` + `answer` 加权平均 | 不传或想看综合分（**默认**） |
| `status` | 只看 agent 是否最终 `status == completed` | 检验"能不能跑通" |
| `tool` | 只看 agent 是否调用过 `expected_tool`（case meta 里指定） | 检验"会不会选工具" |
| `answer` | 只看文本相似度（`SequenceMatcher`） | 检验"答案对不对" |

> **历史名字已废弃**：早期 README 里的 `exact` / `contains` / `exact_match` / `regex` / `tool_called` / `numeric_tolerance` 都**不是**当前 API 接受的 `scorer` 字段值。`backend/app/eval/metrics.py` 里确实有这些打分函数，但它们仅在 `EvalRunner` 内部被 `Metrics.exact_match` 之类的字段引用，**不**暴露给 `/api/v1/agents/{id}/evals` 的请求体。
>
> ⚠️ `scripts/tests/passing_eval.py` 和 `scripts/tests/eval_e2e.py` 里还残留着 `scorer: "contains"`，跑前要改成 `composite`（详见 [`scripts/README.md`](../scripts/README.md)）。

详细原理看 §7。**对应操作**：[§8.6 出考卷](#86-给服务员出考卷evaluation-tab)

---

## 4. 服务员的工作循环 loop.py

这是整个项目**最有意思的一段逻辑**。**先看一段伪代码，然后用比喻说**。

```python
# backend/app/agent/loop.py（简化）
async def run_agent(agent, user_input, budget):
    messages = [{"role": "system", "content": agent.system_prompt},
                {"role": "user",   "content": user_input}]
    steps = []

    for i in range(budget.max_steps):
        # 1. 把当前对话扔给大脑，让它决定下一步
        response = await llm.chat(messages, tools=agent.tools)
        step = {"index": i, "kind": "think", "output": response.text}
        steps.append(step)

        # 2. 如果大脑说"我调用 X 工具"
        if response.tool_call:
            for call in response.tool_calls:
                result = await execute_tool(call.name, call.args)
                step2 = {"index": i, "kind": "tool_call", "name": call.name, "args": call.args}
                step3 = {"index": i, "kind": "tool_result", "output": result, "err": result.error}
                steps += [step2, step3]
                messages.append({"role": "tool", "content": result.output})

        # 3. 如果大脑说"最终答案在这里"
        elif response.final_answer:
            step = {"index": i, "kind": "final_answer", "output": response.text}
            steps.append(step)
            return Run(status="completed", steps=steps, output=response.text)

    return Run(status="max_steps_reached", steps=steps)
```

用餐厅的话说，就是**服务员接到点单后的标准工作流程**：

1. **听单** —— 客人说"来一份 12 乘以 (3 加 4)"，服务员把单子记到小本本上
2. **想一步** —— 看看小本本上的对话历史，琢磨"我该干嘛"
3. **动手** ——
   - **要工具** → 从腰包里掏家伙（calculator），按一下，把结果写回小本本
   - **要交卷** → 把最终答案写下来，告诉老板"做完了"
4. **再想一步** —— 看看新的小本本，再琢磨"还要不要干嘛"
5. **循环** —— 回到第 2 步，直到"交卷"或"小本本写满"

**重点**：服务员**不会自己造工具**。腰包里有什么，他只能用那些。**这就是为什么"工具"和"大脑"是两个独立的东西**——大脑负责"想"，工具负责"做"。

循环里**有 6 种 step（步骤）**，每种在数据库里占一行，UI 上也对应一种图标（见 §8.5）：

| step kind | 含义 | UI 图标 |
| --- | --- | --- |
| `think` | 大脑吐出来的话（推理 / 文本） | 🧠 |
| `tool_call` | 大脑说"我要用 X 工具" | 🛠 |
| `tool_result` | 工具返回的结果 | 📥（带 `error` 时变 ❌） |
| `final_answer` | 收工，交卷 | ✅ |
| `observation` | 辅助信息（如 memory 写入、错误回显） | ℹ️ |
| `error` | 这一步出错了 | ❌ |

**对应操作**：[§8.4 派活给 agent](#84-派活给-agentagent-run-tab) → [§8.5 看 trace](#85-看-trace-区域的过程回放)

---

## 5. 工具腰包 tools.py

`backend/app/agent/tools.py` 里现在装着 9 个内置工具，**用一个装饰器就能注册**：

```python
from app.agent.tools import tool

@tool()
def calculator(expression: str) -> str:
    """Evaluate a Python expression safely in a sandbox."""
    return str(eval_in_sandbox(expression))
```

这一行就在"工具登记表"里挂上了一个 `calculator`，`/api/v1/tools` 接口会自动把它列出来，前端 AgentConfig 页面的 Tools 下拉菜单也会出现它。

### 5.1 9 个内置工具一览

| 名字 | 功能 | 安全等级 | 例子 |
| --- | --- | --- | --- |
| `echo` | 原样返回输入 | 安全 | `echo("hi") -> "hi"` |
| `now` | 返回当前 ISO 时间 | 安全 | `now() -> "2024-01-15T10:30:00Z"` |
| `calculator` | 安全沙箱里跑 Python 表达式 | 安全 | `calculator("12*3+4") -> 40` |
| `http_request` | HTTP GET/POST 请求（超时 10s） | 中等 | `http_request("GET https://api...")` |
| `search_knowledge` | 检索长期记忆（默认 exact 匹配；启用 ChromaDB 后走 cosine） | 安全 | `search_knowledge(query="deadline", top_k=5)` |
| `file_read` | 读文件 | 中等 | `file_read(path="/tmp/x.txt")` |
| `file_write` | 写文件 | 中等 | `file_write(path="/tmp/x.txt", content="...")` |
| `shell_exec` | 执行 shell 命令 | 高 — 默认在 sub-process 隔离 | `shell_exec(cmd="ls")` |
| `final_answer` | 让 agent 提交结果 | 安全 | `final_answer(text="42")` |

### 5.2 工具的"安全等级"是怎么保证的？

- **安全**：纯计算 / 字符串处理，**不会触碰你的电脑**
- **中等**：会读/写文件或发 HTTP，**但限制在沙箱目录或带超时**
- **高**：`shell_exec` —— 这是唯一能"动你系统"的工具
  - **默认模式**：在子进程里跑，**不会污染主进程**
  - **Docker 模式**：在独立容器里跑，**完全隔离**
  - 切换方法：`.env` 里设 `AGENTFORGE_SANDBOX_MODE=docker`（**注意：环境变量叫 `_MODE`，不是 `AGENTFORGE_SANDBOX`**，见 `docker-compose.yml:38` 和 `.env:37`）

### 5.3 加新工具 — 见 §9

加新工具 3 步搞定（一个 `@tool()` 装饰器 + 重启后端 + 在前端勾上），**详见 §9**。

**对应操作**：[§8.2 建 agent 时勾选 Tools](#82-建第一个-agentconfiguration-tab) · [§8.3 改工具](#83-改工具说明当前版本要删建)

---

## 6. 服务员的小本本 memory.py

服务员有两个"本本"：

- **短期小本本** = 当前 Run 的 `messages` 列表（**跟着 Run 走，Run 结束就没了**）
- **长期小本本** = 数据库 `memory` 表（**跨 Run 保留，agent_id 是 key**）

短期小本本就是 §4 那段循环里那个 `messages` 变量——前面所有的对话 + 工具调用 + 工具结果全在里面。

长期小本本只记**"重要的事"**，由 3 个 API 管：

| HTTP | 路由 | 干啥 |
| --- | --- | --- |
| `GET` | `/api/v1/agents/{id}/memory` | 看这个 agent 记住了什么 |
| `POST` | `/api/v1/agents/{id}/memory` | 手动加一条（**API 默认 kind="episodic" importance=0.5**；**前端默认 kind="manual" importance=0.5**——文档/前端/后端三处默认值不一致，详见 §A.4 踩坑提示） |
| `DELETE` | `/api/v1/agents/{id}/memory/{mem_id}` | 删一条 |

`search_knowledge` 工具会从长期小本本里**搜**内容（exact match + cosine similarity 双路径），所以只要在 system_prompt 里写"用 search_knowledge 查 memory"，agent 就能在每次 run 时自动用到之前存的东西。

**对应操作**：[§8.5 写长期记忆](#85-看-trace-区域的过程回放) 的"步骤 6：给服务员写一条长期记忆"小节

---

## 7. 评测系统：出题、收卷、批改

老板的考核办公室一共 3 道工序：

### 7.1 出题 — `eval/runner.py::EvalRunner.run()`

接收一个**试卷**（一组 EvalCase），挨个让 agent 跑，**把每一题的结果**记到 `EvalCase` 表里。

一道题的形状：

```json
{
  "input": "12 * (3 + 4) 等于多少",
  "expected": "84",
  "expected_tool": "calculator"
}
```

### 7.2 收卷 — `eval/runner.py` 循环

每跑完一题，更新 `EvalRun` 的 `done` / `passed` / `failed` 计数。前端通过 `GET /evals/{id}/progress` 拉取实时进度（**每 1500ms 轮询一次**——见 `Evaluation.tsx` 的 `setTimeout(r, 1500)`）。

### 7.3 批改 — `eval/scorer.py::get_scorer(name)`

`scorer` 字段决定用什么尺子量。**4 种合法名字**（已废弃的 `exact` / `contains` 等见 §3.6 的告警）：

| 名字 | 公式（简化） | 适用场景 |
| --- | --- | --- |
| `composite` (默认) | `status`、`tool`、`answer` 三个子分**算术平均**（无加权） | 想看综合分 |
| `status` | 1.0 if `agent.status==completed` else 0.0 | 检验"能不能跑通" |
| `tool` | 1.0 if 调用了 `expected_tool` else 0.0 | 检验"会不会选工具" |
| `answer` | `SequenceMatcher(None, expected, output).ratio()` | 检验"答案对不对" |

`pass_threshold`（代码默认 **0.7**）决定 EvalCase 是否算"通过"；`EvalRun.passed` 是通过题数 / 总题数。

**对应操作**：[§8.6 出考卷](#86-给服务员出考卷evaluation-tab)

---

## 8. 一键端到端跑一遍（网页全程）

> **这一节是"操作实战"——讲每个按钮在哪里、点完之后会看到什么。**
> 原理部分见 §3-§7；想从原理读起，**回到 §1 顺着看**。

### 8.0 前置：把服务跑起来

`[对应原理 §3.5]`

**步骤 0：把 AgentForge 装到你的电脑上**

1. **装 Docker Desktop**：去 docker.com 下载 Windows 版，装完双击启动。等鲸鱼图标变绿。
2. **确认 .env 文件存在**：打开 `C:\Users\86159\Desktop\agentforge` 文件夹，找一个叫 `.env` 的文件（**没有扩展名**）。
   - 如果没有就**右键 → 新建 → 文本文档** → 改名 `.env`（Windows 不让改就先打开"查看"→ 勾"文件扩展名"）
   - 用记事本打开，粘贴：
     ```
     AGENTFORGE_LLM_PROVIDER=mock
     AGENTFORGE_MAX_STEPS=10
     AGENTFORGE_MAX_TOKENS=8000
     ```
   - 保存
3. **启动服务**：在项目根目录打开 PowerShell / CMD，跑：
   ```bash
   docker compose up -d --build
   ```
4. **看是否成功**：
   ```bash
   docker compose ps
   ```
   看到 **4 个容器**（`agentforge-api` / `agentforge-frontend` / `agentforge-sandbox` / `agentforge-chromadb`）都是 `running` / `healthy` 就 OK。
5. **打开浏览器**：
   - 前端网页 → http://localhost:5173
   - 后端 API 文档 → http://localhost:8000/docs（Swagger UI）

**Windows 11 家庭版专属踩坑**：如果双击 Docker Desktop 看到红字 `Virtualization support not detected`，或者鲸鱼一直是橙色 `Engine stopped`，那是**硬件虚拟化没开 + WSL 没装**。修复步骤：
1. 开机进 BIOS（重启按 F2/Del），找 `SVM` / `VT-x` / `Virtualization`，设为 Enabled
2. 重启进 Windows，**管理员模式**打开 PowerShell，跑：
   ```powershell
   wsl --update
   wsl --install -d Ubuntu
   ```
3. 重启，Docker Desktop 再双击一次

### 8.1 进入浏览器

**你做的动作**

- 打开浏览器（Chrome / Edge / Firefox 都行）
- 地址栏输 `http://localhost:5173` → 回车

**你看到的东西**

- 页面顶部有三个 tab：**Configuration**（默认选中）/ **Agent Run** / **Evaluation**
- 中间是 **"New Agent"** 卡片，下面是已有的 agent 列表（**第一次打开是空的**）
- 整个页面是暗色 / 浅色主题（取决于你系统设置）

**这代表啥**

- 三个 tab 对应后端三个主子模块：建员工、派活、出考卷（§3.5）
- 第一次空表是正常的——一行还没建

### 8.2 建第一个 Agent（Configuration tab）

`[对应原理 §3.3 Agent 模板]`

**你做的动作**

1. 在 **New Agent** 卡片里，从上到下填：
   - **Name**：随便起，比如 `小张`（**必填**，唯一）
   - **Description**：一句话描述，可选，比如 `数学老师`
   - **Model**：下拉菜单选 `mock-model`（默认就是它）
   - **System Prompt**：守则，比如 `你是一个数学老师，所有计算用 calculator`
   - **Tools**：**勾上 3 个 chip**：`calculator` + `final_answer` + `echo`
2. 鼠标点 **Create** 按钮

**你看到的东西**

- 按钮变 `Creating...` 然后恢复
- **下面那张 Agent 表格立刻多出一行**：
  - name=`小张`
  - model=`mock-model`
  - tools=`calculator, final_answer, echo`（逗号分隔的 chip）
  - 最右边有 **Delete** 按钮

**这代表啥**

- 这张表里的一行 = 数据库 `agents` 表里的一行（§3.3）
- 勾选的 Tools 决定了这个 agent 的"腰包"（§5）
- `name` 必须唯一——重名会报 400

### 8.3 改工具（说明：当前版本要删建）

**⚠️ UI 限制**：当前版本的 Configuration 页面**没有"编辑已有 agent"功能**（UI 还没做编辑按钮）。
要改 Tools / System Prompt / Model，**只能 Delete 掉再 Create 一个新的**。

### 8.4 派活给 Agent（Agent Run tab）

`[对应原理 §4 loop.py]`

**你做的动作**

1. 鼠标点顶部 **Agent Run** tab
2. 页面切到第二个 tab
3. **Agent** 下拉菜单：选刚才建的 `小张`
4. **Input** 框：输入一个问题，比如 `echo 早上好 然后 final_answer`
5. **TokenGauge**（横条）：会显示当前 run 累计用了多少 token，**总预算是写死的 8000**（不能在 UI 里改）
6. 鼠标点 **Run** 按钮

**你看到的东西**

- 按钮变 `Running...`
- 中间 Trace 区域开始冒东西（可能 1-2 秒延迟）
- 右边出 Status 徽章：`running`（蓝）→ `completed`（绿） / `failed`（红）
- 下面出 **TokenGauge**（横条），实时显示用了多少 / 总共多少（**总预算是写死 8000**）

**这代表啥**

- 这一次 run = 数据库 `runs` 表里的一行（§3.3）
- TokenGauge 显示的是**整个 run**累计的 token 用量，对应 `Run.token_usage` 字段
- 如果 5 分钟还没出 Status，**看 §A.4 排查**

### 8.5 看 Trace 区域的过程回放（前半）

`[对应原理 §4 步骤 + §3.6 step kind 颜色]`

**你看到的东西**

Trace 区域会**冒出一个步骤树**，长得像这样（**真实输出**）：

```
[step 1] 🧠 think
  → "I'll echo and then call final_answer"
[step 1] 🔧 tool_call: echo("早上好")
[step 1] 📥 tool_result: "早上好"
[step 2] 🧠 model output
  → "Now I'll submit the final answer"
[step 2] 🏁 final_answer: "done"
```

每种 step 有不同的小图标：
- 🧠 **think** = 大脑想了一句话
- 🛠 **tool_call** = 大脑要掏工具了
- 📥 **tool_result** = 工具返回结果（含 `error` 时变 ❌）
- ✅ **final_answer** = 收工，交卷
- ℹ️ **observation** = 辅助信息（memory 写入、错误回显等）
- ❌ **error** = 这一步出错了

**可以再玩一次**

- 改 Input 框的内容，比如改成 `echo 早上好`，再点 Run
- Trace 区域会换成新的内容，下面的 history 卡片会**多出一行**——这是这一单的记录
- 想回看前面那一单：点 history 里的那一行，前端的 Trace 区域会自动加载那一单的内容

---

**步骤 6：给服务员写一条长期记忆**

`[对应原理 §6 memory.py]`

**你做的动作**

- 滚到 Agent Run 页面最底下，看到 **Memory** 卡片
- 卡片顶端写着 "Memory · 0 条"（目前 0 条）
- 中间有一个输入框 **"Add a memory..."** 和一个 **Add** 按钮
- 在输入框里点一下，输入：`用户的名字叫 Alice`
- 按回车键，**或者**用鼠标点 **Add** 按钮

**你看到的东西**

- 按钮变 `Saving...` 然后恢复
- 卡片里冒出一行新记忆：
  ```
  用户的名字叫 Alice
  kind: manual · importance: 0.50
  ```
  右边有一个小 **Delete** 按钮

**这代表啥**

- 这条记忆进了"长期小本本"（§6），**以后任何一单 Agent Run 都会自动带上它**——前提是 agent 的 system_prompt 写明了要用 memory
- "kind: manual" 是说这条是你手动加的（不是 agent 自己"学"的；**注意**前端用的是 `manual`，后端 API 默认是 `episodic`，跟 §3.3 / §6 不完全一致）
- "importance: 0.50" 是这条记忆的"重要程度"分数，0 到 1，越大越重要（**注意**代码默认值是 0.5，不是 0.85）
- 想删掉：点这条记忆右边的 **Delete** 按钮

### 8.6 给服务员出考卷（Evaluation tab）

`[对应原理 §7 eval 系统]`

**你做的动作**

- 鼠标点顶部 **Evaluation**（考核）tab
- 页面切到第三个 tab

**你看到的东西**

- 顶部有一行下拉菜单 + 输入框：
  - **Agent** 下拉菜单：默认是 `小张`
  - **Dataset** 输入框：默认 `default`
  - **Scorer** 文本输入框：默认值是 `contains`（**注意**：当前 UI 是文本框不是下拉，**没有** `composite` 这个选项，但你也可以直接输 `composite`——后端会认）
  - **Pass threshold** 数字输入框：默认 0.8
- 中间是一张 **Test Cases**（考题表），默认就有两行：
  - 第 1 行：input=`What is 2 + 2?` / expected=`4`
  - 第 2 行：input=`Capital of France?` / expected=`Paris`
- 表格右上角有一个 **Add case**（加题）按钮
- 表格每行最右边有一个 **×** 删除按钮
- 表格下面是 **Start** 按钮
- 最下面是 **Past Evaluations**（历史考卷）卡片，目前是空的

**这代表啥**

- 这张表就是你要给服务员做的"考卷"（§7）
- 一行就是一题，左边是题目，右边是标准答案
- "Scorer" 是**改卷的尺子**（§3.6 / §7.3）——文本框里的值就是"用哪种尺子量"。**后端只认 4 个名字**（`composite` / `status` / `tool` / `answer`），详见 §3.6 告警
- "Pass threshold" 是**及格线**——平均分高于这个数算通过

**你做的动作（继续）**

- 题目不用改，删掉第 2 行（点那一行最右边的 **×**）—— 第 1 题 2+2，mock 大脑答不出真的数学答案，预期会挂，但没关系先看流程
- 鼠标点 **Start** 按钮
- 按钮变 `Running...`
- 下方 Past Evaluations 卡片里**冒出一行新记录**：
  - dataset=`default`
  - status 徽章是橙色的 `running`
  - pass_rate 列暂时是 `0/1 (0%)`

**然后等几秒**

- 几秒后，status 徽章从橙色 `running` 变成**绿色** `completed`（或者**红色** `failed`，取决于运行结果）
- pass_rate 变成最终的数字，比如 `0/1 (0%)` 或 `1/1 (100%)`

**这代表啥**

- 整张卷子跑完了
- 想看每道题的详情（哪一步扣了分、为什么），切到 http://localhost:8000/docs 用 Swagger UI 拉 `/evals/{id}` 详情接口

### 8.7 一次完整流程的小结

| 步骤 | 在哪 | 干啥 | 点啥 | 看啥 | 对应原理 |
| --- | --- | --- | --- | --- | --- |
| 1 | 浏览器 | 打开前端 | 地址栏输 `localhost:5173` | 三个 tab，停在 Configuration | §8.1 |
| 2 | Configuration | 建服务员 | Create | 表格多一行 | §3.3 / §8.2 |
| 3 | Configuration | 改工具 | （目前要删掉重建） | — | §5 / §8.3 |
| 4 | Agent Run | 派活 | Run | Trace 区域开始闪 | §4 / §8.4 |
| 5 | Agent Run | 看过程 | （不用点，等） | Steps 横条 / Trace 树 | §4 / §8.5 |
| 6 | Agent Run | 加记忆 | Add | 长期本多一行 | §6 / §8.5 |
| 7 | Evaluation | 出考卷 | Start | Past Evals 表格多一行 | §7 / §8.6 |

**全流程不需要写一行代码。**

### 8.8 一些"踩坑提示"（网页版特有）

| 你看到 | 是什么意思 | 怎么办 |
| --- | --- | --- |
| 顶部 tab 一直是空白 | 前端进程没起 | 看 §8.0 / 附录 A.1，启动前端 |
| 表格里一直 `loading...` | 后端没起 | `curl http://localhost:8000/health` 试试 |
| 点 Run 后 Trace 不动 | mock 大脑没装好 | 重新看 §8.0，确认 `.env` 里 `AGENTFORGE_LLM_PROVIDER=mock` |
| Evaluation 的 Scorer 字段是文本输入框不是下拉 | 当前 UI 还没做下拉选择 | 直接输 `composite` / `status` / `tool` / `answer` 之一即可（原理见 §3.6 / §7.3） |
| 切 tab 之后页面数据没变 | 浏览器缓存 | Ctrl+Shift+R 硬刷新 |
| 点 Delete 之后表格没立刻少一行 | 后端在处理，前端在等 | 刷一下页面就行 |
| Trace 区域显示 `tool_result.err` | 工具调用失败 | 看 §A.4 "ctx 串扰"那段，是后端代码问题 |
| Status 徽章一直是 `running` 超过 5 分钟 | 卡住了 | 刷新页面、看后端日志（见 §A.4） |

---

## 9. 加一个新工具（开发入门）

`[对应原理 §5 tools.py]`

> **这一节是给"想动代码的人"看的**——零基础用户可以跳过，工具栏里 9 个内置工具够用。

加一个新工具 3 步搞定：

### 9.1 写函数

在 `backend/app/agent/tools.py` 末尾加：

```python
@tool()
def get_weather(city: str) -> str:
    '''Return the current weather for a city.'''
    # ... 调你的 API ...
    return f"{city}: 22C, sunny"
```

**规则**：
- 函数名 = 工具名（蛇形或驼峰都行）
- 参数和返回值类型要写好（装饰器会读它们生成 schema）
- 第一行 docstring = 工具描述（**这个描述会被发给 LLM 让它知道这个工具干嘛**）

### 9.2 重启后端

```bash
docker compose restart agentforge-api
```

### 9.3 在前端勾上

回到 §8.2 — 在 New Agent 卡片里 Tools 一栏会**自动多出**一个 `get_weather` chip，勾上就行。

**对应操作**：[§8.2 建 agent](#82-建第一个-agentconfiguration-tab)

---

## 10. 总结 + 路线图

### 10.1 一句话总结

**agentforge = 一个给 AI 服务员配工具、布置任务、批改作业的"管理工作台"，全部本地跑，不依赖云服务。**

### 10.2 三个核心心智模型

1. **"小餐厅"比喻**——老板 / 服务员 / 腰包 / 小本本 / 前台 / 后厨 / 收银抽屉 / 考核本
2. **"四步循环"**——听单 → 想一步 → 动手（要工具 / 要交卷）→ 再想一步
3. **"两步记忆"**——短期（跟着 Run 走）vs 长期（跟着 Agent 走）

### 10.3 路线图

- [ ] 重写 `MemoryStore._search_embed`，换成连接外部 vector service（ChromaDB / pgvector）
- [ ] 加个 `OpenAI-compatible` 适配器，以便连 DeepSeek / Qwen / 任何 OpenAI-format 接口
- [ ] Eval 结果页面加热图，逐频度查看
- [ ] WebSocket 推送，跑任务时实时刷新步骤
- [ ] Agent 文件导入 / 导出（YAML / JSON）
- [ ] Configuration 页面加"编辑已有 agent"按钮（目前要删建，见 §8.3）
- [ ] Evaluation 页面 Scorer 下拉里加 `composite` 选项（目前 UI 还停在旧名字）

---

## 附录 A. 深度体验 + 出错怎么办 + 概念速查

### A.1 真模型配置（用 OpenAI / Anthropic）

打开 `.env`，把 `AGENTFORGE_LLM_PROVIDER` 改成 `openai` 或 `anthropic`，再填一个对应的 `*_API_KEY`：

```
AGENTFORGE_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
AGENTFORGE_OPENAI_MODEL=gpt-4o-mini
```

> `.env` 里的默认模型是 `gpt-4o-mini`（省钱）；改 `gpt-4o` / `gpt-4.1` 都行，前端 Model 下拉已经写死这 4 个选项。

然后：

```bash
docker compose restart agentforge-api
```

前端 New Agent 卡片的 **Model** 下拉里**写死**了 4 个选项：`gpt-4o-mini` / `gpt-4o` / `gpt-4.1` / `claude-3-5-sonnet`（见 `AgentConfig.tsx:85-91`）。**`gpt-4o` 默认就在下拉里**，**不需要**改 env 才会"冒出来"。**但 mock-model 仍然可用**——你可以拿它当 baseline 跟真模型 PK。

> ⚠️ 真模型花钱。`Token Budget` 默认 8000 够用一两次演示，但**别忘了改 Pass threshold 和限制 case 数量**。

### A.2 直接用浏览器调 API（Swagger UI）

`[对应原理 §3.5 api/ 路由]`

不用前端网页，**浏览器直接打开** `http://localhost:8000/docs`：

- 看到一个 FastAPI 自动生成的"所有 API 的表单页面"
- 每个接口都可以点 **"Try it out"** → 填 JSON → 点 **Execute** → 看返回
- 想看 POST `/api/v1/agents` 的请求体长啥样？左边展开它就有现成模板

### A.3 用 curl 直接调 API

```bash
# 1. 建一个 agent
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{"name":"curl-小张","provider":"mock","model":"mock-model",
       "system_prompt":"x","tools":["echo"]}'

# 2. 拿到返回里的 id，再派活
curl -X POST http://localhost:8000/api/v1/agents/<AGENT_ID>/runs \
  -H "Content-Type: application/json" \
  -d '{"input":"echo hi"}'

# 3. 看考试详情
curl http://localhost:8000/api/v1/agents/<AGENT_ID>/evals/<EVAL_ID>
```

### A.4 出问题了怎么办

`[对应原理 §3.6 + §8.8]`

**后端起不来**：
1. `docker compose ps` —— 是不是 **4 个**容器（`agentforge-api` / `agentforge-frontend` / `agentforge-sandbox` / `agentforge-chromadb`）都 running？
2. `docker compose logs --tail 200 agentforge-api` —— 看最后 200 行报错
3. `docker compose down -v && docker compose up -d --build` —— 推倒重来

**前端白屏**：
1. F12 打开开发者工具，看 Console 红字
2. 看 Network tab，`/api/v1/agents` 是不是 200？

**某次 Run 报 500**：
1. `docker compose logs --tail 200 agentforge-api | grep ASGI` —— 找 traceback
2. 用 `scripts/diagnostics/backend_errors.py` 一键 dump ASGI 上下文：
   ```bash
   python scripts/diagnostics/backend_errors.py
   ```

**Trace 区域显示 `tool_result.err`**：
- 这是 §4 里 "ctx 串扰" 问题——`messages` 列表在多 step 时顺序乱了
- 临时方案：在前端 input 里写得更明确（比如 "use calculator then echo"）
- 长期方案：看 `loop.py` 第 ~80 行的 `messages.append` 顺序

**Eval 报 422 "scorer not supported"**：
- 你可能用了 `exact` / `contains` 这种**已废弃**名字
- 改成 `composite` / `status` / `tool` / `answer` 之一（见 §3.6）

**Status 徽章卡在 `running` 超过 5 分钟**：
- 99% 是 §4 循环里死循环了
- 看后端日志里 `step[i] model` 是不是在重复同一条

### A.5 七个最重要的概念

| 概念 | 比喻 | 干什么的 | 对应原理 |
| --- | --- | --- | --- |
| **Agent（员工）** | 雇一个 AI 服务员 | 有一个名字、一份守则、一包工具 | §3.3 |
| **Run（任务）** | 给员工派一次活 | 输入一个问题，得到一个答案 | §3.3 / §4 |
| **Step（步骤）** | 员工思考/动手的一个小动作 | `model` / `tool_call` / `tool_result` / `final_answer` | §4 / §8.5 |
| **EvalCase（考题）** | 一张试卷上的一道题 | 题目 + 答案 + 期望方法 | §3.3 / §7 |
| **EvalRun（一次考试）** | 给员工做一套题 | 跑完全部题，算总分 | §3.3 / §7 |
| **Scorer（评分员）** | 改卷老师 | 给每道题打 0~1 分 | §3.6 / §7.3 |
| **Sandbox（沙箱）** | 员工做事的小黑屋 | 程序在小黑屋里跑，不会伤到你的电脑 | §5.2 |

### A.6 出现"docker compose ps"看到 exited 怎么办？

按顺序试：
1. `docker compose restart <服务名>` —— 重启那个服务
2. `docker compose logs <服务名>` —— 看它为什么挂
3. `docker compose down -v && docker compose up -d --build` —— 推倒重来

---

## 附录 B. 完整目录结构（你看到的文件夹里都有啥）

> **这是最新（2026-08-25 整理过）的版本**——早期版本把脚本、日志、文档全堆在根目录，现在已经收进子目录。

```
agentforge/                                ← 你打开的这个文件夹
│
├── README.md                              ← 英文产品说明
├── docker-compose.yml                     ← 一键启动的"总开关"
├── .env                                   ← 环境变量（运行时读）
├── .gitignore                             ← 告诉 Git 别管哪些文件
│
├── docs/                                  ← 📖 所有中文文档
│   ├── README.md                          ←   文档目录索引
│   ├── 从零读懂 agentforge（含操作步骤）.md ←   你正在读的这份
│   └── _section8_old.txt                  ←   §8 草稿备份（不影响阅读）
│
├── logs/                                  ← 🪵 构建/运行日志
│   ├── build.log / build2.log / ...       ←   docker compose build 的输出
│   ├── docker_up.log / docker_rebuild*.log←   启动/重建的完整输出
│   ├── docker_frontend.log                ←   前端容器日志
│   └── .last_agent                        ←   上次选中的 agent id 缓存
│
├── scripts/                               ← 🛠 工具脚本（详见 scripts/README.md）
│   ├── README.md                          ←   脚本索引（哪个干啥的）
│   ├── tests/                             ←   正经测试
│   │   ├── smoke_test.py                  ←     烟雾测试（create→run→eval→memory）
│   │   ├── passing_eval.py                ←     通过型评测
│   │   ├── list_expected.py               ←     列表型 expected 验证
│   │   ├── eval_e2e.py                    ←     端到端评测
│   │   └── poll_eval.py                   ←     轮询小工具
│   └── diagnostics/                       ←   一次性排查工具
│       ├── backend_errors.py              ←     dump ASGI 错误上下文
│       ├── debug_eval.py                  ←     探测 /evals 各种 body 形状
│       ├── find_init.py                   ←     在 backend 里搜 create_all 等
│       ├── dump_slice.py                  ←     读 .md 指定行号范围
│       ├── scan_doc.py / scan_doc2.py     ←     扫文档里的陈旧 API/scorer 名
│       └── scorer_names.py                ←     列 scorer.py 里所有名字
│
├── backend/                               ← 后端（Python 大脑）
│   ├── app/
│   │   ├── agent/                         ← Agent 循环、工具、记忆
│   │   │   ├── loop.py                    ←   主循环（最核心的代码）
│   │   │   ├── tools.py                   ←   9 个内置工具 + @tool 装饰器
│   │   │   ├── context.py                 ←   上下文窗口管理
│   │   │   ├── memory.py                  ←   长时记忆
│   │   │   └── sandbox.py                 ←   安全小黑屋
│   │   ├── api/                           ← HTTP 接口
│   │   │   ├── agents.py                  ←   员工管理
│   │   │   ├── runs.py                    ←   派活
│   │   │   ├── evals.py                   ←   考试
│   │   │   ├── memory.py                  ←   记忆 CRUD
│   │   │   └── tools.py                   ←   列工具
│   │   ├── eval/                          ← 评测系统
│   │   │   ├── runner.py                  ←   跑评测的人
│   │   │   ├── scorer.py                  ←   评分标准
│   │   │   └── metrics.py                 ←   相似度算法
│   │   ├── main.py                        ← 后端启动入口
│   │   ├── database.py                    ← 数据库连接
│   │   ├── models.py                      ← 数据表结构
│   │   ├── schemas.py                     ← Pydantic 模型
│   │   └── config.py                      ← 读 .env
│   ├── openapi.json                       ← FastAPI 自动生成的 API 元数据
│   ├── tests/                             ← pytest 测试
│   ├── requirements.txt                   ← Python 依赖清单
│   └── pytest.ini
│
├── frontend/                              ← 前端（网页脸面）
│   ├── src/
│   │   ├── pages/                         ← 三个主页面
│   │   │   ├── AgentConfig.tsx            ←   员工管理
│   │   │   ├── AgentRun.tsx               ←   派活大厅
│   │   │   └── Evaluation.tsx             ←   考试中心
│   │   ├── components/                    ← 小零件
│   │   │   ├── StepNode.tsx               ←   步骤卡片
│   │   │   ├── TraceTree.tsx              ←   步骤树
│   │   │   ├── ToolCallCard.tsx           ←   工具调用卡片
│   │   │   └── TokenGauge.tsx             ←   Token 仪表盘
│   │   ├── stores/
│   │   │   └── agentStore.ts              ←   全局状态（Zustand）
│   │   ├── api/
│   │   │   └── client.ts                  ←   跟后端对话的小客服
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── package.json
│   └── vite.config.ts
│
├── docker/                                ← Docker 镜像配方
│   ├── backend.Dockerfile                 ←   后端的"菜谱"
│   ├── frontend.Dockerfile                ←   前端的"菜谱"
│   └── (sandbox 上下文在 ../sandbox)
│
├── sandbox/                               ← 安全小黑屋的镜像
│   └── Dockerfile
│
└── data/                                  ← 运行时数据（数据库文件等）
```

---

## 附录 C. 写给想贡献代码的人

PR 欢迎！主要思路：

1. Fork 本仓库
2. 开个分支：`git checkout -b feat/awesome-tool`
3. 加个测试：放 `backend/tests/test_your_thing.py`（pytest 风格）或 `scripts/tests/`（脚本风格）
4. 改完跑一遍：`cd backend && pytest -q`
5. 提 PR，描述你的使用场景和为什么这个变化重要

**License**: [MIT](../LICENSE) · 开源，随你怎么用。

---

## 写在最后

如果跟着这份文档走完一遍，你应该已经：
- ✅ 知道 agentforge 是什么、为什么这样设计（§1-§7）
- ✅ 成功把 agentforge 装到电脑并启动了（§8.0）
- ✅ 雇了一个 AI 员工，配置了他的工具和守则（§8.2）
- ✅ 给他派了几个活，看他一步一步干活（§8.4-§8.5）
- ✅ 给他出了一张考卷，看他得了多少分（§8.6）
- ✅ 知道哪里出问题去哪里查（§A.4）
- ✅ 知道怎么加新工具（§9）

接下来你可以：
- **多加几个员工**，把不同员工拉出来 PK
- **多出几套考卷**，把考卷做大、做全
- **加几个新工具**（§9），给员工配更全的腰包
- **改改代码**，让 agentforge 变成你想要的样子

**agentforge 是一个"可生长的玩具"——它能陪你从零基础玩到能做产品。**

祝你玩得开心。🎉

**新店开张，生意兴隆。**

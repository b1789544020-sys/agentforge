# `scripts/` — 脚本总览

> 这个文件夹里**只有"工具脚本"**，不是产品代码。
> 产品代码在 `backend/` 和 `frontend/`。
>
> 这里的脚本分两类：`tests/` 是**正经测试**，每次改完代码后可以跑一下验证；`diagnostics/` 是**一次性排查工具**，正常情况下用不到，遇到怪问题时再翻出来。

---

## 📂 `scripts/tests/` — 5 个正经测试脚本

跑测试前先确认：后端已经起来（`http://localhost:8000/health` 返 200），数据库已经建好。

| 脚本 | 干啥 | 一句话命令 | 最近一次结果 |
| --- | --- | --- | --- |
| `smoke_test.py` | **烟雾测试**——一口气跑 create agent → run → eval → eval detail → memory → list tools，任何一步出现 4xx/5xx 就立刻在打印里看出 | `python scripts/tests/smoke_test.py` | **6/6 通过** |
| `passing_eval.py` | **通过型评测**——用 `composite` scorer 跑一题，验证最终 `status=completed` 且 `passed>=1` | `python scripts/tests/passing_eval.py` | **1/1 passed** |
| `list_expected.py` | **列表型 expected**——验证 `expected` 字段支持 `["hi","hello"]` 这种多答案列表 | `python scripts/tests/list_expected.py` | **2/2 passed** |
| `eval_e2e.py` | **端到端评测**——一次跑两题（`hi`/`world`），观察 progress 轮询 → final 详情 | `python scripts/tests/eval_e2e.py` | 待跑 |
| `poll_eval.py` | **轮询小工具**——对一个 hard-coded agent id（`6999e908-...`）的最近一次 eval 拉 `/progress` 40 次 | `python scripts/tests/poll_eval.py` | 工具脚本，不计分 |

### ⚠️ 已知坑：两个脚本还在用废弃的 scorer 名

`passing_eval.py` 和 `eval_e2e.py` 里都还写着 `scorer_name: "contains"`，但 **`contains` 早就从 `scorer.py::get_scorer` 移除了**（当前合法名字是 `default` / `composite` / `status` / `tool` / `answer`）。
跑这两个脚本前**先把脚本里的 `contains` 改成 `composite`**，否则会拿到 422 报错。
这一轮没改是因为：先把目录收拾干净，下一轮再批量修脚本。

---

## 📂 `scripts/diagnostics/` — 7 个一次性排查小工具

这些是**遇到问题时**才掏出来的工具，不是日常用的。**文件夹名已经写明"diagnostics"（诊断）**——别误以为是产品代码。

| 脚本 | 干啥 | 什么时候用 |
| --- | --- | --- |
| `backend_errors.py` | `docker logs --tail 2000 agentforge-api` 然后把含 `ASGI` 那一段（含前后 30 行上下文）打出来 | 后端 5xx 报错、想看 traceback 上下文 |
| `debug_eval.py` | 用 4 种不同 body 形状去 POST `/api/v1/agents/{id}/evals`，看每种的 HTTP 状态码和返回 | 评测接口报 400/422、又说不清为啥 |
| `find_init.py` | 在 `backend/app/` 里全文搜 `create_all` / `create_async` / `init_db` 等关键词 | 想知道 SQLite 表是在哪一行建的 |
| `dump_slice.py` | 读一个 `.md` 文档的指定行号范围（`python dump_slice.py 700 750`） | 长文档定位某一段 |
| `scan_doc.py` | 在操作说明文档里扫 `scorer_name`、API 路径等关键词，列出来带行号 | 检查文档里有没有**陈旧的接口/字段名** |
| `scan_doc2.py` | `scan_doc.py` 的简化版，只关心 `/api/v1/runs/`、`scorer_name:"exact"` 几个高危 pattern | 同上，更快 |
| `scorer_names.py` | 进 `agentforge-api` 容器 `grep` 一把 `scorer.py`，列出所有 scorer 名字 | 想确认 `get_scorer()` 当前到底支持哪些名字 |

> 共同点：所有 diagnostic 脚本都**假设后端在 `localhost:8000`**（直接 `urllib` 调 API）。要远程用就把 `BASE` 改一下。

---

## 怎么加新脚本？

- 新的"产品级测试"（每次跑 CI 都要的）→ 放 `scripts/tests/`，**加进本表"正经测试"那张表**
- 新的"临时排查工具"（用一次就丢的）→ 放 `scripts/diagnostics/`，**加进下面这张表**
- 千万别往根目录丢 `.py` —— 根目录只放配置/编排文件（`.env`、`.gitignore`、`docker-compose.yml`）

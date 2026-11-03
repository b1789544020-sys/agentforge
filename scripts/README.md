# `scripts/` — 验证、运维与离线工具

产品代码位于 `backend/` 和 `frontend/`。本目录只保存辅助脚本，不是另一套产品实现。涉及真实服务、Docker、Provider 或压测的脚本必须在目标环境运行并保留输出，不能把“脚本存在”写成“已验收”。

## 自动化与端到端验证

| 脚本 | 用途 | 命令 |
| --- | --- | --- |
| `tests/quality_gate_e2e.py` | 隔离临时 DB 中验证首版全过、次版两条失败及 diff | `python scripts/tests/quality_gate_e2e.py` |
| `tests/smoke_test.py` | 运行中服务的 Agent/Run/Eval/Memory/Tool 烟雾检查 | 先启动后端，再 `python scripts/tests/smoke_test.py` |
| `tests/passing_eval.py` | 通过型评测示例 | `python scripts/tests/passing_eval.py` |
| `tests/list_expected.py` | 多答案 expected 示例 | `python scripts/tests/list_expected.py` |
| `tests/eval_e2e.py` | 评测 progress 到终态的演示 | `python scripts/tests/eval_e2e.py` |
| `tests/poll_eval.py` | 轮询指定历史 Eval 的诊断工具 | 先按实际 ID 修改脚本，不纳入验收计数 |
| `validation/verify_stack.py` | health/readiness + 一个真实 Agent 运行并输出 JSON 证据 | `python scripts/validation/verify_stack.py --output evidence/stack.json` |

主测试集仍以 `backend/tests/` 为准：

```bash
python -m pytest backend/tests -q
python -m pytest backend/tests/test_direction1.py backend/tests/test_direction2.py \
  backend/tests/test_direction3.py backend/tests/test_direction4.py backend/tests/test_direction5.py -q
```

## 私有化与离线物料

| 脚本 | 用途 |
| --- | --- |
| `offline/prepare.py` | 联网准备 wheel、npm cache、Docker image、源码和 SHA-256 manifest |
| `offline/verify_bundle.py` | 离线包完整性与哈希校验 |
| `offline/install.sh` | 目标 Linux 断网安装，`pip --no-index`、`npm --offline`、`docker load` |
| `offline/start_standalone.py` | 同一应用的 SQLite + memory + inprocess 单进程启动 |
| `security/secret_scan.py` | 扫描 tracked source 中疑似明文密钥，不打印匹配值 |

```bash
python scripts/offline/prepare.py dist/offline
python scripts/security/secret_scan.py
python scripts/validation/verify_stack.py --provider ollama \
  --model qwen2.5:3b --endpoint http://ollama.internal:11434/v1
```

验证令牌只通过 `AGENTFORGE_VERIFY_TOKEN` 环境变量注入。物料必须匹配目标 OS、CPU 架构和 Python ABI。

## 备份、恢复与性能

- `backup.py` / `restore.py`：SQLite/PostgreSQL 备份恢复；恢复覆盖数据，先停止写入并显式确认。
- `perf/locustfile.py`、`perf/k6-smoke.js`：short/ramp/spike/soak 有界压测。
- `perf/large_eval.py`：大数据集评测。保留原始结果、队列、资源和 p95，不写无数据结论。

详见 `docs/PRODUCTION.md`、`docs/PERFORMANCE.md` 和 `docs/OFFLINE.md`。

## 一次性诊断

`diagnostics/` 包含日志抓取、接口形状、文档关键词和 scorer 排查工具。它们可能假设 `localhost:8000` 或包含历史示例 ID，只用于人工诊断，不属于持续回归，也不应作为生产证据。

新增脚本时：持续行为验证放 `tests/` 或 `validation/`，一次性排查放 `diagnostics/`，密钥不得硬编码；任何“最近一次结果”只有在真实执行并留存输出后才能更新。

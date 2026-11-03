# AgentForge 性能验证计划

## 场景与目标

所有场景均有明确时长/VU 上限，生产数据先备份。建议初始门槛（不是实测结果）：HTTP 失败率 `<1%`、p95 `<500ms`、无未解释 5xx、队列不持续增长、RSS/CPU/DB 连接数在预算内。真实 LLM 延迟应单独统计，不能与 mock 指标混用。

| 场景 | 目的 | 配置 |
| --- | --- | --- |
| short | 30 秒基线 | 2 VU |
| ramp | 阶梯负载与连接池观察 | 2→5→10 VU，约 3 分钟 |
| spike | 瞬时峰值与队列恢复 | 5 秒内升至 20 VU，保持 25 秒 |
| soak | 内存泄漏、任务堆积、TTL/恢复 | 10 VU，5 分钟起步，正式验收建议 30 分钟以上 |
| large eval | 大数据集调度/DB/Provider 压力 | 默认 100 case，可显式调整，最多 10000 |

## Locust

```bash
pip install locust
LOAD_SHAPE=short locust -f scripts/perf/locustfile.py --host http://localhost:8000 --headless
LOAD_SHAPE=ramp  locust -f scripts/perf/locustfile.py --host http://localhost:8000 --headless
LOAD_SHAPE=spike locust -f scripts/perf/locustfile.py --host http://localhost:8000 --headless
LOAD_SHAPE=soak  locust -f scripts/perf/locustfile.py --host http://localhost:8000 --headless
```

## k6

```bash
SCENARIO=short k6 run scripts/perf/k6-smoke.js
SCENARIO=ramp  k6 run scripts/perf/k6-smoke.js
SCENARIO=spike k6 run scripts/perf/k6-smoke.js
SCENARIO=soak  k6 run scripts/perf/k6-smoke.js
```

脚本内置 `http_req_failed <1%`、`p95 <500ms` 阈值。JWT 通过 `PERF_TOKEN` 注入；不要把 token 写入命令历史或结果文件。

## 大数据评测

```bash
python scripts/perf/large_eval.py --count 100 --concurrency 4
# 如需复用已有 Agent 和 JWT：
python scripts/perf/large_eval.py --agent-id <ID> --token <JWT> --count 1000
```

记录每组：版本/配置、实例数、Provider 与模型、VU、时长、RPS、平均值/p50/p95/p99、错误率、队列峰值、DB 连接数、CPU/RSS。保留原始结果文件，不能只写“性能良好”。

## 本轮状态

当前工作区没有运行中的生产服务，因此**尚未产生实测压测结果**。完成 Docker 与 Provider 环境后按上述顺序执行，任何未达标项应回到实现排查，不得用文字总结替代数据。

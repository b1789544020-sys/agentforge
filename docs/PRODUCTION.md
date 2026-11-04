# AgentForge 生产部署与运维手册

## 1. 验收状态（必须先读）

本仓库已完成代码改造、离线单元/集成测试、Alembic 升降级、前端类型检查与生产构建。**通过测试不等于生产验收完成**。以下项目必须在目标环境另行验证：

- Docker Compose 全栈实际启动、PostgreSQL/Redis/Qdrant 连通及重启恢复。
- 真实 Docker 沙箱的断网、只读文件系统、资源限制和容器逃逸复核。
- 本地 `bge-small-zh` 模型加载与中文检索质量；hashing 只是确定性降级，不是语义模型。
- Kimi、GLM、豆包、OpenAI、Anthropic 等真实 Provider 的 API key、配额、模型名和连通性。
- 短基线、ramp、spike、soak 压测与大数据评测指标。
- 真实 Redis 多 worker 公开交付限流、受信代理网段、Nginx CSP 渲染和浏览器 iframe 行为；离线共享计数替身不能替代这些验收。

## 2. 环境准备

1. 安装官方 Docker Desktop（Windows 使用 WSL 2 后端），准备 Python 3.11、Node.js 20。
2. 复制 `.env.example` 为 `.env`。
3. 用密码管理器生成高熵 `AGENTFORGE_SECRET_KEY`，不要提交 `.env`。
4. 按宿主执行 `getent group docker`（Linux）设置 `DOCKER_GID`。Docker socket 只挂载给 API 与 Celery worker；不要挂载到执行沙箱。
5. 配置实际 Provider 的独立 API key。未配置时 mock 只用于开发演示。
6. 可选真实中文向量：
   ```bash
   pip install -r backend/requirements-embedding.txt
   # 准备本地 BAAI/bge-small-zh-v1.5 模型文件，然后：
   AGENTFORGE_EMBEDDING_PROVIDER=sentence_transformers
   AGENTFORGE_EMBEDDING_MODEL=/absolute/path/to/bge-small-zh-v1.5
   ```
   默认 `EMBEDDING_LOCAL_FILES_ONLY=true`，缺少模型时必须显式报错，不能悄悄退回 hashing。

## 3. 启动与停止

```bash
docker compose up --build -d
docker compose logs -f migrate
docker compose ps
```

启动顺序：PostgreSQL 健康 → 一次性 `migrate` 成功 → 双 worker API 健康 → Celery worker/Beat。前端是 Nginx 静态构建，同时代理 HTTP 与 WebSocket。

- 前端：`http://localhost:5173`
- liveness：`http://localhost:8000/health/live`
- readiness：`http://localhost:8000/health/ready`
- 实例指标：`GET /api/metrics`（管理员/JWT 或静态管理员 token）

停止写入但保留数据：

```bash
docker compose stop agentforge-api celery-worker celery-beat agentforge-frontend
```

完全停止（不要加 `-v`，否则删除数据库卷）：`docker compose down`。

## 4. 用户、团队与数据隔离

首位注册用户是 super-admin，后续为用户。通过 Organization → Team → Membership 分配 team-admin/developer/viewer。普通用户只能访问所属团队及本人旧个人空间的 Agent、Run、Eval、Memory、Suite、知识库、模板、交付和运营数据；跨团队猜测 ID 与 WebSocket 订阅同样拒绝。静态 token 是兼容管理员主体，不应分发给普通用户。

危险工具默认关闭。开启前配置 `AGENTFORGE_FILE_ALLOWED_DIRS`、`AGENTFORGE_HTTP_ALLOWED_HOSTS`，保留 `AGENTFORGE_SANDBOX_REQUIRED=true`，并走 ApprovalRequest 管理员授权；Docker 不可用时拒绝执行，禁止宿主降级。

Agent config 中的模型密钥落库前加密。生产建议独立注入高熵 `AGENTFORGE_ENCRYPTION_KEY`，并与 JWT `AGENTFORGE_SECRET_KEY` 分别纳入密钥管理；API、导出和日志只保留脱敏值。团队、审计和审批详情见 `docs/TEAMS.md`，离线/密钥细节见 `docs/OFFLINE.md`。

## 5. 备份（每次升级前）

先停止 API 和 Celery 写入。SQLite：

```bash
python scripts/backup.py --database-url sqlite+aiosqlite:///./backend/agentforge.db
```

PostgreSQL（宿主需有 `pg_dump`）：

```bash
python scripts/backup.py --database-url "$AGENTFORGE_DATABASE_URL"
```

同时按 Qdrant 官方流程创建 volume snapshot，并记录 Redis AOF 是否属于需要保留的 broker 状态。将备份复制到另一台机器并做一次恢复校验；只有文件存在不代表备份可用。

## 6. 回滚演练

1. 记录当前应用版本、Alembic head、备份校验和。
2. 停止所有写入进程。
3. 将 schema 降到备份对应版本（示例 revision 必须替换）：
   ```bash
   docker compose run --rm --no-deps migrate \
     sh -c "cd backend && python -m alembic downgrade <TARGET_REVISION>"
   ```
4. 恢复已验证备份：
   ```bash
   python scripts/restore.py --input backups/<BACKUP> \
     --database-url "$AGENTFORGE_DATABASE_URL" --yes
   ```
5. 恢复 Qdrant snapshot，启动旧版本应用，检查 readiness、核心 API 和数据条数。
6. 恢复 Celery 前清理或隔离旧 broker 中不再兼容的任务。持久任务采用 at-least-once，恢复后需检查是否发生重放。

所有 `0001` 至当前迁移都有 downgrade；先在临时环境演练，生产不要直接试验。

## 7. 日志、指标与恢复

- 每个 HTTP 请求有 `X-Request-ID`；访问日志为 JSON，500 响应也带 request id。
- `/api/metrics` 提供实例 QPS、错误率、平均/p95 延迟、评测队列和 Celery broker 深度。多实例必须分别采集，不能把一个实例的值当全局值。
- Run 每个完整迭代提交 RunState；恢复保留上下文、下一步、token、累计步数和时长。
- Celery late-ack + worker-lost requeue + 每任务 Redis 锁提供 at-least-once 执行。Eval 中断重放会删除部分结果再跑，避免重复计数。
- Beat 清理 TTL 记忆并标记超时任务，扫描定时质量门禁并评估运营告警。检查 worker 日志和队列后再决定是否人工重放。

## 8. 上线前检查清单

- [ ] 全栈全新启动与卷保留重启成功，readiness 全绿。
- [ ] migration upgrade/downgrade/restore 在临时环境完成。
- [ ] 两个用户及 WebSocket 的跨租户越权复核通过。
- [ ] 真实 Docker 沙箱断网/只读/资源限制复核通过。
- [ ] 真实 embedding 与所有启用的 Provider 连通通过。
- [ ] 公开交付在真实 Redis 多 worker 下按 token/IP 限流，代理 CIDR 与 CSP 白名单已收敛，Nginx `nginx -t` 和浏览器三形态通过。
- [ ] 压测达到书面 p95、错误率和资源上限目标。
- [ ] 备份异地保存且恢复演练通过。

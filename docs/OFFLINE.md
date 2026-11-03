# 私有化与离线部署

## 状态边界

仓库提供离线物料打包、校验、安装和目标环境验证脚本。**当前工作区未在真实断网机器上完成全栈验收**，也未实测 Ollama/vLLM 模型效果；下述命令必须在目标环境留存输出证据。

## 外网依赖与离线方案

| 依赖 | 来源 / 锁定 | 离线方案 |
| --- | --- | --- |
| Python 后端与测试包 | `backend/requirements.txt`，精确版本 | 联网机 `pip download` 到 `wheels/`，目标机 `pip --no-index` |
| 可选中文 embedding | `backend/requirements-embedding.txt` | 按目标 Python/CPU/OS/ABI 单独下载 wheel；模型文件放 `models/` |
| 前端 npm | `frontend/package-lock.json` | 联网机 `npm ci --cache npm-cache`，目标机 `npm ci --offline` |
| 后端/前端/沙箱镜像 | `docker/backend.Dockerfile`、`docker/frontend.Dockerfile`、Compose | 联网机构建并 `docker image save`，目标机 `docker load` |
| PostgreSQL / Redis / Qdrant / Nginx | `postgres:16-alpine`、`redis:7-alpine`、`qdrant/qdrant:v1.12.4`、Compose 中 Nginx 锁定 tag | 同一 image tar，Compose `pull_policy: never` |
| Python 基础镜像 | Dockerfile 中固定 tag | 构建时准备；目标机不重新 build |
| 本地 LLM / embedding 模型 | 客户提供的 Ollama/vLLM 或本地模型目录 | 人工拷贝并记录模型版本、SHA-256、算力后端；禁止运行时公网下载 |
| 字体/词表/tiktoken | 应用使用系统字体，token 估算可降级 | 需要精确 tokenizer 时随模型物料附带，不访问公网 CDN |

**物料必须匹配目标 OS、CPU 架构和 Python ABI。** Windows 准备的 wheel 不能直接用于麒麟 Linux；x86 镜像不能用于 ARM。跨平台应在同架构联网准备机执行打包，或为 pip 增加目标 platform/ABI 参数并逐一验证。

## 制作离线包

在已联网、已安装 Python/npm/Docker/git 的准备机，先提交目标版本，再执行：

```bash
python scripts/offline/prepare.py dist/offline
```

产物包括 `wheels/`、`npm-cache/`、`images/agentforge-images.tar`、`source/agentforge-source.tar`、`manifest.json` 和校验脚本。可选模型由客户放入 `models/` 后，应重新计算并登记校验值。传输前验证：

```bash
python dist/offline/scripts/verify_bundle.py dist/offline
```

## 断网安装

将完整目录复制到目标机，只需要本地 Python 3、npm 和 Docker：

```bash
bash scripts/offline/install.sh /media/agentforge-offline /opt/agentforge
cd /opt/agentforge/src
python3 scripts/validation/verify_stack.py --output evidence/offline.json
```

安装脚本使用 `pip --no-index`、`npm ci --offline`、`docker load` 和 `docker compose --no-build`，并叠加 `docker-compose.offline.yml` 的 `pull_policy: never`。首次启动生成本地随机 JWT 密钥，默认 mock/auth 关闭只用于离线验收；生产应立即启用认证并限制网络入口。

验证脚本检查 `/health`、`/health/ready`，创建并运行一个 mock Agent 后清理。若使用独立令牌，通过环境变量 `AGENTFORGE_VERIFY_TOKEN` 注入，不要写进命令历史。

## Ollama / vLLM 本地推理

Agent 使用以下配置，协议为 OpenAI `POST {base_url}/chat/completions`：

```json
{
  "provider": "ollama",
  "model": "qwen2.5:3b",
  "config": { "base_url": "http://ollama.internal:11434/v1" }
}
```

vLLM 将 `provider` 改为 `vllm`，endpoint 改为其 `/v1` 根路径。也支持 `local` / `openai-compatible`，此时必须显式配置 `config.base_url`。`config.endpoint` 是等价别名。本地 Provider 没有 key 时使用 `local-not-required` 占位，不会静默退回 mock；只有显式 mock 才允许假模型。Docker 内访问宿主 Ollama 时，应配置宿主的内网地址或 `host.docker.internal`，不能使用容器自身 localhost。

目标机验证（非 mock）：

```bash
python3 scripts/validation/verify_stack.py \
  --provider ollama --model qwen2.5:3b \
  --endpoint http://ollama.internal:11434/v1 \
  --output evidence/ollama.json
```

## 敏感配置与密钥

- 环境注入独立 `AGENTFORGE_ENCRYPTION_KEY`（Fernet key）；未设置时由高强度 `AGENTFORGE_SECRET_KEY` 派生。生产建议两个密钥独立并纳入 KMS/密钥轮换流程。
- Agent config 中 `api_key`、`*_token`、`*_secret`、`*_password` 等字符串在落库前加密，API/导出仅显示 `********`，运行时在使用点解密。更新中的 `********` 保留旧密文。
- JSON 访问日志和异常文本过滤 Bearer、密钥赋值、手机号、身份证号。不要把密钥放在 URL、命令行或 trace 输入中。
- 扫描：`python scripts/security/secret_scan.py`。扫描只报告文件、行号和规则，不打印匹配密钥。

## 零运维单机路径

只改环境，不换代码：SQLite + memory task/control + inprocess vector + 单 worker。

```bash
cp .env.standalone.example backend/.env
python scripts/offline/start_standalone.py
# 另一个终端
python scripts/validation/verify_stack.py --output evidence/standalone.json
```

该路径没有 Redis/Qdrant/Celery/PostgreSQL，适合边缘验证和小团队，不提供分布式持久队列与横向扩展；生产全量形态继续沿用主 Compose。

# 信创 OS / 硬件支持矩阵

## 验证状态定义

- **已验证**：必须有目标组合上的安装日志、健康检查和一次非人工伪造的 Agent 运行证据。
- **理论兼容未测**：依赖存在对应平台版本，但本仓库没有目标环境证据。
- **暂不支持**：当前没有可执行的适配路径，或必须依赖未实现的专用驱动。

当前工作区是 Windows x86 开发环境，**没有任何信创组合的“已验证”记录**。普通离线 pytest 结果不能冒充麒麟/UOS/国产算力验收。

| OS / 硬件 | 推理路径 | 状态 | 说明与验证物 |
| --- | --- | --- | --- |
| 麒麟 V10 + x86_64 + CPU | 同架构 Docker + Ollama/vLLM CPU | 理论兼容未测 | 需在同架构准备 wheel/image，执行下方验证脚本 |
| 麒麟 V10 + ARM64 + CPU | ARM64 Docker + ARM 本地推理服务 | 理论兼容未测 | 所有镜像和 wheel 必须为 ARM64；不可复用 x86 物料 |
| 统信 UOS + x86_64 + CPU | 同架构 Docker + Ollama/vLLM CPU | 理论兼容未测 | 需核对 Docker/containerd、systemd 与 glibc 版本 |
| 统信 UOS + ARM64 + CPU | ARM64 Docker + ARM 本地推理服务 | 理论兼容未测 | 需模型运行时的 ARM 构建证据 |
| 国产 Linux + 昇腾 | 昇腾适配的 vLLM/厂商 OpenAI-compatible 服务 | 理论兼容未测 | AgentForge 可连 `/v1`，但不内置 CANN/Huawei 驱动；驱动与模型服务由物料方验证 |
| 国产 Linux + 寒武纪 | 厂商 OpenAI-compatible 推理网关 | 理论兼容未测 | 不内置 Cambricon 驱动；需网关协议与模型实跑证据 |
| 无 OpenAI-compatible 接口的国产加速卡 | 直接 SDK | 暂不支持 | 需另立 Provider 适配和硬件测试，不得宣称开箱即用 |

## 目标环境验证步骤

1. 在同架构准备机制作离线包并核对 `manifest.json`。
2. 断网安装，记录 `uname -a`、`/etc/os-release`、CPU/加速卡、Docker 与驱动版本。
3. 先运行 mock 全栈检查，再运行真实本地 Provider 检查：

```bash
python3 scripts/validation/verify_stack.py --output evidence/xc-mock.json
python3 scripts/validation/verify_stack.py \
  --provider vllm --model your-local-qwen \
  --endpoint http://inference.internal:8001/v1 \
  --output evidence/xc-provider.json
```

4. 将两份 JSON、安装日志和平台信息归档。只有全部通过后才能把对应矩阵行改为“已验证”。

`verify_stack.py` 输出包含平台、架构、健康/readiness、Agent 创建和真实运行结果。它会删除测试 Agent，不记录 API key；令牌仅从 `AGENTFORGE_VERIFY_TOKEN` 环境变量读取。

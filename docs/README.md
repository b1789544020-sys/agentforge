# AgentForge 文档导航

> 当前能力与启动方式见根目录 [README](../README.md)；技术验证记录和后续迭代集中在 `docs/planning/`。本页提供各专题入口。

## 快速入口

| 目的 | 入口 | 说明 |
| --- | --- | --- |
| 了解并启动项目 | [根目录 README](../README.md) | 产品能力、架构、配置与启动命令 |
| 零基础理解项目 | [从零读懂 AgentForge](<从零读懂 agentforge（含操作步骤）.md>) | 中文入门导览；界面和命令以当前版本为准 |
| 观看产品展示 | [产品展示视频](demo.md) | Agent 调试、固定回归、四层证据、人工复核和固定版本交付 |
| 理解版本回归与门禁 | [质量门禁与版本回归](QUALITY_GATES.md) | AgentVersion、EvalSuite、GateRun 和版本 diff |
| 了解四层质量证据 | [16.10 数据模型与实施记录](planning/QUALITY_DATA_MODEL.md) | 输出/工具/检索/系统证据、版本 diff、反馈关联和 0012 迁移 |
| 查看产品开发顺序 | [开发路线图](../开发路线图.md) | 17 步路线、执行批次、任务卡和 DoD |
| 查看 P0 实际基线 | [开发基线与能力矩阵](planning/DEVELOPMENT_BASELINE.md) | 环境、测试、真实 Provider、浏览器证据与未决边界 |
| 查看 Agent Schema v1 契约 | [Agent Schema v1 设计](planning/AGENT_SCHEMA.md) | 设计评审、16.8 后端前置契约及最小单 Agent Studio 均已落地；增强型工坊仍属后续范围 |
| 查看本地研究助理候选包 | [16.9 专项设计](planning/LOCAL_RESEARCH_ASSISTANT.md) / [公开示例包](../examples/local-research-assistant/README.md) | 公开资料、Agent Definition、评测用例和质量验证计划 |
| 复盘真实失败 | [故障案例库](troubleshooting/README.md) | 中文记录失败背景、复现、根因、修复、无效尝试与脱敏日志 |
| 查看后续迭代方向 | [后续开发规划](planning/FUTURE.md) | 真实模型质量、Studio 增强、生产验收和首次体验研究 |

## 使用、交付与运维

| 文档 | 内容 |
| --- | --- |
| [DELIVERY.md](DELIVERY.md) | 一键交付、模板与私有知识库 |
| [OPERATIONS.md](OPERATIONS.md) | 运营指标、成本、坏样本与告警 |
| [OFFLINE.md](OFFLINE.md) | 私有化与离线部署 |
| [PRODUCTION.md](PRODUCTION.md) | 生产部署与运维 |
| [PERFORMANCE.md](PERFORMANCE.md) | 性能验证计划与证据要求 |
| [TEAMS.md](TEAMS.md) | 多团队 RBAC、审计与高风险授权 |
| [XC_SUPPORT.md](XC_SUPPORT.md) | 信创 OS / 硬件支持矩阵及验证状态 |

## 中文导读与早期材料

以下文件保留原文，便于理解项目演进；其中早期“调试与评测工作台”等定位不应覆盖根目录 README 的当前说明。

- [这个项目实际是什么](这个项目实际是什么.md)
- [项目介绍](项目介绍.md)
- [从零读懂 AgentForge](<从零读懂 agentforge（含操作步骤）.md>)

## 演示与制作

- [产品展示](demo.md)：展示视频、功能画面和快速开始入口。
- `assets/agentforge-quality-loop/`：审核后的操作截图、逐场景解说词与媒体验收记录。
- [后续开发规划](planning/FUTURE.md)：真实模型质量、Studio 增强、生产验收与首次体验研究。
- [视频制作记录](planning/QUALITY_LOOP_VIDEO.md)：原始素材、剪辑过程和技术验收历史。
- 根目录 `AgentForge-demo.mp4/.srt/.gif`：可随仓库展示的 MP4、独立字幕与 GIF 预览。

## 规划与历史材料

- [规划区索引](planning/README.md)：当前执行计划、公开示例入口和历史材料边界。
- 本机 `.scratch/`、运行日志和一次性诊断脚本不进入公开仓库；正式故障复盘收录在[故障案例库](troubleshooting/README.md)。

## 根目录与文件归位约定

| 位置 | 保留内容 | 不放在这里的内容 |
| --- | --- | --- |
| 项目根目录 | README、LICENSE、CHANGELOG、Compose、环境示例、项目配置 | 工作令、视频草稿、一次性测试输出 |
| `backend/` / `frontend/` | 产品源码及各自测试 | 文档草稿和录屏素材 |
| `docs/` | 面向使用者的专题文档与精选公开素材 | 原始录屏、TTS 音频和中间合成文件 |
| `docs/planning/` | 当前计划与执行记录 | 运行日志和临时调试脚本 |
| `scripts/demo/` / `scripts/tests/` | 可复现的媒体合成与浏览器验收脚本 | 一次性本机诊断脚本 |
| `data/` / `sandbox/` | 运行数据与沙箱定义 | 文档整理产物 |

根目录 Compose 文件被测试和文档直接引用，不能仅为减少根目录文件数量而移动。新增文档优先放入现有分类并更新本索引，不再建立平行导航。

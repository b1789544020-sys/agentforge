# 证据文件说明

这些文件来自 2026-11-10 本机 P0/16.1 验收。为避免把数百行重复堆栈塞进文档，这里保存的是**关键摘录**，不是伪装成完整日志的“成功记录”。文件头标明原始退出码或摘录性质，绝对路径已替换为 `<repo>`、`<home>`、`<venv>`。

| 文件 | 当时正在做什么 | 结果 |
| --- | --- | --- |
| `01-python314-install-error.log` | 在 Python 3.14.6 干净 venv 安装原 `backend/requirements.txt` 与 editable 项目 | 编译 tiktoken、aiohttp、pydantic-core 失败，退出码 1 |
| `02-fastapi-204-import-error.log` | 使用 Python 3.12.10 和原 pin 首次收集/运行后端套件 | FastAPI 0.115.6 在导入 204 路由时断言，套件无法开始 |
| `03-pytest-asyncio-ordering-error.log` | 修复 204 后运行完整后端套件 | 140 failed、210 passed、48 rerun；主线程无当前事件循环 |
| `04-sqlite-memory-lifetime-error.log` | 移除自定义 event loop 后再次运行完整后端套件 | 349 passed、1 failed；Memory TTL 用例找不到 `memories` 表 |
| `05-global-router-introspection-error.log` | 在全局 FastAPI 0.141 环境回归新增路由测试 | 349 passed、1 failed；内部 `_IncludedRouter` 与旧版扁平路由表不同 |
| `06-fixed-verification.log` | 应用测试基础设施修复后做定向与完整验证 | 最终命令和计数以该文件及上级 README 为准 |

完整本机日志还保存在未跟踪的 `.scratch/roadmap-baseline-2026-11-10/`。该目录可能包含庞大的安装器日志和机器路径，因此不直接入库；本目录收录足以复核根因的持久摘录。没有截取或伪造 GUI 图片：这些失败均发生在终端，原始文本比截图更适合搜索和复现。

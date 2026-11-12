# P0/16.1：原依赖 pin、Python 版本与异步测试基础设施故障

> 日期：2026-11-10
> 环境：Windows 11 x64；对比 Python 3.14.6 全局环境与隔离 Python 3.12.10 venv
> 目标：不升级原依赖 pin、不跳过测试、不放宽产品断言，关闭后端干净环境验收
> 状态：**已解决**。pinned Python 3.12 与全局 Python 3.14 对照环境均为 350 passed、0 failed；详见第 7 节和 `evidence/06-fixed-verification.log`

## 1. 为什么当时会遇到这些失败

路线图 P0/16.1 要求证明新环境可以安装项目原有依赖，并在该环境运行完整后端套件。此前 Python 3.14.6 全局环境曾得到 349 passed，但全局 pytest、FastAPI、Pydantic、SQLAlchemy 与项目 pin 不同，**这只能证明那组全局包下的兼容性，不能替代原 pin 验收**。

本次工作依次经历了四层失败：安装不上、应用导入不了、异步测试按顺序运行成片失败、最后暴露出 SQLite 内存库生命周期问题。它们不是同一个根因，不能用一次“依赖升级”笼统掩盖。

| 阶段 | 动作 | 实际结果 |
| --- | --- | --- |
| A | Python 3.14.6 干净 venv 安装原 requirements | 缺少 cp314 wheel，转源码构建；无 MSVC linker，退出码 1 |
| B | Python 3.12.10 + 原 pin 首次启动 pytest | FastAPI 0.115.6 对显式 204 路由响应模型断言，导入中断 |
| C | 修复 204 后跑完整套件 | 140 failed / 210 passed / 48 rerun；后续 async 测试找不到主线程 loop |
| D | 移除自定义 loop 后跑完整套件 | 349 passed / 1 failed；周期 Memory 清理后 `memories` 表消失 |

证据见 [`evidence/`](evidence/README.md)。本案例没有 GUI 截图，因为失败均发生在 pip/pytest 终端；保留的是可搜索的真实输出，不用后期示意图冒充现场。

## 2. 失败 A：Python 3.14 安装原 pin 需要本机编译工具链

### 复现

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
```

当时 `python` 是 3.14.6。日志中的关键结果是：

```text
error: Microsoft Visual C++ 14.0 or greater is required
error: linker `link.exe` not found
Failed to build tiktoken aiohttp pydantic-core
error: failed-wheel-build-for-install
```

### 根因与决策

这是**Python ABI 与旧锁定包 wheel 覆盖范围**问题，不是简单的网络错误，也不是 pip 本身损坏。原 pin 的部分原生包没有可用于 cp314 的现成 wheel，pip 因此尝试 C/Rust 源码构建；机器没有 MSVC C++ Build Tools，构建失败。

可选解法本来有两条：安装完整 MSVC/Rust 构建工具链，或选用已有兼容 wheel 的 Python 版本。本次采用 **Python 3.12.10 隔离运行时**，保留 `backend/requirements.txt` 原样，不借升级 FastAPI/Pydantic 等直接 pin 来制造“绿色”。这不等于宣布项目永远不支持 3.14；若以后支持，应单独验证源码构建或更新依赖政策。

## 3. 失败 B：FastAPI 0.115.6 在导入阶段拒绝 204 响应模型

### 现象与定位

安装成功后 pytest 连测试收集都无法完成，最短堆栈为：

```text
backend/app/api/tools_metadata.py:291: in <module>
    @router.delete(..., status_code=status.HTTP_204_NO_CONTENT, ...)
fastapi/routing.py:507: in __init__
    assert is_body_allowed_for_status_code(...)
AssertionError: Status code 204 must not have a response body
```

较新的全局 FastAPI 没暴露这一问题，所以之前全局绿色不能发现它。失败发生在 decorator 创建路由时，不是 HTTP 客户端偶尔返回了错误 body。

### 修复

在五处显式 204 DELETE 声明中增加 `response_model=None`，覆盖 tools metadata、Prompt、Knowledge、Gate、Team API；同时在 `test_tools_metadata.py` 增加工具元数据的公开 OpenAPI 契约回归，断言 204 response 不含 `content`。

工具元数据定向测试 **12/12 passed**。另一次 pinned 应用路由表检查显示当前 **14 个 204 路由均无 response model**；这是当前应用检查结果，不冒充“未来新增路由永远自动正确”的保证。修复只明确协议声明，没有删除 204、改成 200 或跳过应用导入。

### 3.1 后续全局 FastAPI 回归又暴露了测试内省问题

新增测试最初直接遍历 `app.routes` 并读取 `route.path`。pinned FastAPI 0.115.6 可以通过，但全局 FastAPI 0.141.1 完整回归变成 **349 passed / 1 failed**：新版路由表含没有 `.path` 的 `_IncludedRouter`。只把访问改成 `getattr(route, "path", None)` 仍不够，因为 lazy included router 不再以旧版扁平形式暴露目标路由，随后失败变成 `StopIteration`。

最终没有针对某个 FastAPI 内部类写适配器，而是改测稳定的**公开 OpenAPI 契约**：

```python
response = app.openapi()["paths"]["/api/tools/{name}/metadata"]["delete"][
    "responses"
]["204"]
assert "content" not in response
```

这套断言在 pinned 与全局环境的工具元数据测试中均为 **12/12 passed**。它说明跨版本回归不仅检查产品，也能发现测试对第三方内部结构的错误依赖。失败原文见 `evidence/05-global-router-introspection-error.log`。

## 4. 失败 C：单测通过，但完整套件出现 140 个事件循环失败

### 为什么要看“顺序”，而不是只重跑失败项

204 修复后，前 44 个用例通过，随后大量互不相关的 API、Knowledge、Tool、WebSocket 用例报同一个错误：

```text
pytest_asyncio/plugin.py:1023: in inner
    _loop = _get_event_loop_no_warn()
RuntimeError: There is no current event loop in thread 'MainThread'.
```

最终为 **140 failed、210 passed、48 rerun**。相同测试在新进程单独运行可以通过，说明不能把它们分别当成 140 个产品功能 bug。真正的共同变量是 pytest-asyncio 0.25.0 的 runner 与测试 fixture 的 loop 生命周期。

最小顺序回归命令：

```powershell
<Python312> -m pytest `
  backend/tests/test_agent_io.py `
  backend/tests/test_anthropic_client.py `
  backend/tests/test_api.py -x -q
```

### 根因与修复

`backend/tests/conftest.py` 自定义了 deprecated session-scoped `event_loop`，覆盖了插件自己负责的 loop 建立和清理过程。在 auto/显式 mark 混合的顺序套件中，该覆盖不再提供 pytest-asyncio 0.25 所期待的生命周期。

修复不是修改 site-packages，也不是在每个测试前随意调用 `asyncio.set_event_loop()`：

1. 删除自定义 `event_loop` fixture，交还插件管理。
2. 显式配置 `asyncio_default_fixture_loop_scope = function`。
3. 保留数据库 fixture 和全部产品断言，不添加 skip/xfail。
4. 用原先会触发顺序失败的组合验证，得到 **42 passed**。

### 一个配置弯路

最初只把 loop scope 加到根 `pyproject.toml`，指定 `backend/tests/test_memory_ttl.py` 时警告仍显示配置未设置。插件报告头显示该命令实际选择：

```text
rootdir: <repo>/backend
configfile: pytest.ini
asyncio_default_fixture_loop_scope=None
```

进一步确认：无参数从仓库根运行全量套件会读取根 `pyproject.toml`；显式传入 `backend/tests/...` 会选择 `backend/pytest.ini`。项目本来就同时维护这两个入口，所以两处现有 asyncio 配置必须同步，并用报告头确认运行值。只看到“文件已经改了”不能证明当前 pytest 进程采用了它。

## 5. 失败 D：周期 Memory 清理后 `memories` 表不存在

移除 loop 覆盖后，完整套件收敛到 **349 passed、1 failed**。`test_memory_ttl.py` 整文件运行可稳定复现，单个周期清理用例独立运行却通过，错误为：

```text
sqlite3.OperationalError: no such table: memories
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: memories
FAILED test_memory_ttl.py::test_periodic_cleanup_runs_and_can_be_cancelled
```

### 为什么不是 TTL 删除逻辑把表删了

产品清理函数只 DELETE 过期行，不执行 DROP TABLE。测试通过 lambda 把同一个 `db_session` 交给周期任务，而 `_cleanup_once` 每轮结束都会关闭传入 session。fixture 使用 named shared-cache in-memory SQLite；这种数据库的生命周期绑定到连接，最后连接关闭会使整个内存库及 schema 消失。function-scoped loop 让这一生命周期问题稳定暴露出来。

### 试过但不够的修复

先把固定 URI `file:agentforge_conftest?...` 改成每次测试唯一的 shared-cache URI。这能防止不同测试共用名称，**但整文件仍然 3 passed / 1 failed**：同一个测试内最后连接关闭导致的生命周期问题依旧存在。因此保留这一无效尝试记录，不把“加了 UUID”误报为修复。

最终把 `db_session` 改为 pytest 临时目录下的**每测试独立文件 SQLite**，保留每连接 `busy_timeout` 和独立 AsyncSession。这样并发/后台关闭连接不会删除数据库文件中的 schema，同时不同测试天然隔离。`test_engine_factory` 也使用唯一 URI，避免固定共享名称。修复后 Memory TTL 整文件 **4 passed**，原断言“过期记录已删除”保持不变。

## 6. 实际变更清单

- `backend/app/api/{tools_metadata,prompts,knowledge,gates,teams}.py`：五处 204 decorator 显式 `response_model=None`。
- `backend/tests/test_tools_metadata.py`：204 路由契约回归。
- `backend/tests/conftest.py`：删除自定义 session loop；`db_session` 使用每测试文件库；runner factory 使用唯一 URI。
- `backend/pytest.ini`、`pyproject.toml`：两个已有测试入口同步声明 function fixture loop scope。
- `docs/troubleshooting/`：保存本案例及脱敏日志摘录。

没有改 `backend/requirements.txt` 来逃避验证，没有跳过失败测试，也没有修改 Memory TTL 产品断言。

## 7. 修复后验证与证据

标准复核命令（把 `<Python312>` 换成隔离 venv 的解释器绝对路径）：

```powershell
<Python312> -m pip check
<Python312> -m pip freeze
<Python312> -c "from app.main import create_app; create_app()"
<Python312> -m pytest backend/tests/test_agent_io.py backend/tests/test_anthropic_client.py backend/tests/test_api.py -x -q
<Python312> -m pytest backend/tests/test_memory_ttl.py -q
<Python312> -m pytest -q
```

| 验证 | 结果 |
| --- | --- |
| 原 pin 安装与 `pip check` | Python 3.12.10 隔离 venv 成功；freeze 与安装器 SHA-256 已保存 |
| 204 定向契约 | 12 passed；14 个挂载 204 路由无 response model |
| 异步顺序最小回归 | 42 passed |
| Memory TTL 文件回归 | 4 passed |
| 第一次修 loop 后完整套件 | 349 passed / 1 failed，作为中间证据保留 |
| pinned 最终完整套件 | **350 passed / 0 failed**，退出码 0，182.98 秒；`python312-backend-full-verified.log` |
| 全局对照最终完整套件 | **350 passed / 0 failed**，退出码 0，130.10s；`python314-backend-full-clean.log` |

两环境最终命令和计数也收录在 `evidence/06-fixed-verification.log`。`compileall` 与 `git diff --check` 通过。scoped Ruff 检查另报告 27 个既有导入/风格问题，本次未批量自动改写无关模块，因此不能宣称后端 Ruff 全绿；这不改变上述 pytest 实际结果。

日志通过 `.scratch/runtime/run_logged.py` 同时保存输出和退出码。不能仅凭终端最后“看起来全绿”就宣称成功，应检查 sidecar exit code 和 pytest 最终汇总。

## 8. 可复用结论与剩余边界

1. **全局环境绿色不等于锁定环境绿色。** 先记录解释器和真实包版本，再解释测试结果。
2. **导入失败、测试运行失败、测试隔离失败要分层处理。** 本案例三种故障有不同根因。
3. **单测通过不能排除顺序依赖。** 至少比较“单用例、所在文件、最小跨文件组合、完整套件”四个层级。
4. **不要修改第三方插件安装目录来修项目测试。** 使用插件支持的 fixture/config 接口。
5. **named in-memory SQLite 不是普通文件库。** 后台任务、session close、engine dispose 都可能影响 schema 生命周期。
6. **失败日志是设计资产。** 它记录了为什么最终方案是这样，避免后续维护者重新引入看似更方便的共享 fixture。

本案例不掩盖其他独立问题：GPU `qwen2.5:3b` invalid CUDA kernel image、前端审计 10 moderate / 1 high 仍需单独处理。Agent Schema v1 仍是评审设计稿，也不因后端测试变绿而变成“已经实现”。

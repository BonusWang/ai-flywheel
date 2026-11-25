# Design: 审批通道抽象与路由拆分

## 决策 1：用 `typing.Protocol` 而不是抽象基类定义通道契约

- **备选方案 A：抽象基类 `ApprovalChannel(ABC)` + 继承**。
  - 缺点：适配器必须反向依赖领域层的基类；现有 `FeishuChannel` 与测试里的 Fake 通道都要改继承关系，属于不必要的侵入。
- **采用方案 B：`@runtime_checkable Protocol`**。
  - 优点：结构化契约，`FeishuChannel` 与任何 Fake 只要方法齐全即满足；`isinstance(obj, ApprovalChannel)` 可在装配点做一次显式检查；契约模块不引入任何实现依赖。
  - 取舍：`runtime_checkable` 只检查成员存在，不校验签名；签名一致性由 `tests/test_approval_channel_abstraction.py` 对照真实调用点固定。

## 决策 2：契约成员以真实调用点为准，不按想象补方法

逐一核对调用点后，`ApprovalChannel` 只包含以下成员：

| 成员 | 调用方 | 用途 |
|---|---|---|
| `availability()` | `runtime._feishu_daemon_status`、`app.py` 自检状态接口 | 守护状态与自检元数据 |
| `first_send(...)` | `orchestrator.py` 挂起分支 | 提问卡首次投递 |
| `handle_click(event)` | `runtime._route_feishu_click` | 点击路由 |
| `start()` / `stop()` | `runtime.start_feishu` / `stop_feishu` | 随 `serve` 生命周期启停 |
| `tick()` | `FeishuChannel._loop`（契约化的发件箱轮询入口） | 补投、重试与回写 |

- `interactive_allowed()` 等 `FeishuChannel` 的内部判定方法**不进入契约**：没有外部调用方，放进去只会扩大实现者的负担。
- 运行时的启动审计原本读 `self.feishu_channel.runner`。`runner` 是飞书专属属性，放进契约会把契约污染成「飞书形状」。运行时在构建时已持有同一个 `self.lark_runner`，审计改读它，契约因此保持通道中立。

## 决策 3：飞书实现下沉到 `adapters/channels/feishu/`，`core/feishu_*` 保留薄再导出

- **备选方案 A：把实现留在 `core/`，只在旁边加一个 Protocol**。
  - 缺点：传输实现（子进程、HTTP 回调、发件箱线程）继续压在领域层，契约只是名义上的。
- **采用方案 B：整体下沉 + 兼容再导出**。
  - 优点：领域层只剩契约；`adapters/channels/` 与 `adapters/runners/`、`adapters/wiki/` 同级，依赖方向一致；既有 `from ai_flywheel.core.feishu_* import ...` 全部继续可用。
  - 兼容层用**显式再导出**而不是 `import *`：对象身份可断言，`tests/test_wave3_contracts_and_hygiene.py` 里「已删除符号不得复活」的检查也不会被通配导入带回来。

## 决策 4：忙碌错误放进契约模块，`ApprovalLockBusy` 成为子类

- 问题：`FeishuChannel` 受理点击后需要区分「另一批次持锁」与「迟到的回答」。原实现从 `core.orchestrator` 导入 `ApprovalLockBusy`——实现下沉后这会变成适配器反向依赖编排器模块。
- 方案：`core/approval_channel.py` 定义 `ApprovalBusyError`（携带 `lock_run_id` / `lock_expires_at`），`core/orchestrator.py` 的 `ApprovalLockBusy` 继承它；通道只 `except ApprovalBusyError`。
- 效果：`raise ApprovalLockBusy(...)`（编排器与既有测试）与 `except ApprovalBusyError`（通道）在运行时是同一条异常链；`app.py` 仍按 `ApprovalLockBusy` 映射 409，门户语义不变。

## 决策 5：`app.py` 拆分为四个 `APIRouter` 工厂，SSE 与 SPA 留在装配层

- **备选方案 A：按 HTTP 方法或 URL 前缀机械拆分**。
  - 缺点：一个业务动作的读写被拆到不同文件，阅读成本更高。
- **采用方案 B：按业务域拆分四个工厂**：`overview`（公开层 / 站点元信息 / 接入与设置）、`governance`（政策、规则、错题、分歧、指标、知识库与 Skill）、`runs`（会话与 P00–P07 运行，含后台启动与后台审批）、`automations`（调度、Dream、飞书自检）。
  - 工厂签名统一为 `build_xxx_router(rt: FlywheelRuntime) -> APIRouter`：路由只依赖运行时容器，不持有模块级全局状态，测试可对同一个 `rt` 重复建 app。
  - SSE `/api/events` 需要 `Request` 与事件循环，SPA 托管需要 `app.mount` 与兜底顺序，二者留在 `app.py`；`/{full_path:path}` 必须最后注册。
  - 路由函数**原样搬运**（函数名、参数、状态码、`detail` 文案、Pydantic 请求体都不改），因此 OpenAPI 的 operationId 与前端契约保持不变。

## 决策 6：路由表用测试逐条固化

`tests/test_approval_channel_abstraction.py` 里保存拆分前的 53 条应用路由（`/health`、`/llms.txt` 与 51 条 `/api/*`），断言 `create_app()` 注册的集合与之一致。这样「少了一条路由」「方法写错」都会立刻变成红灯，而不是等前端点到 404 才发现。

## 验证策略

- **自动测试（`test`）**：契约一致性、旧/新导入同源、`ApprovalLockBusy` 属于契约忙碌错误、53 条路由逐条一致；并回归 `test_feishu_group1/2`、`test_arch_invariants`、`test_control_plane_console`、`test_automation_console`。
- **真实进程（`real`）**：在隔离的临时 `--base-dir` 与独立端口启动 `flywheel serve`，真实请求 `/health`、`/llms.txt`、`/api/site/overview`、`/api/site/runs`、`/api/site/automations` 与一个 404 分支，确认拆分后的进程装配、路由与 MCP 挂载可用；不触发任何真实模型调用或知识写入。
- **一键闸门（`test`）**：`openspec validate --all --strict` 与 `python scripts/verify_repo.py` 全绿。

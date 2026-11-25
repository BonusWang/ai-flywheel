# Proposal: 审批通道抽象与 Feishu 适配器下沉

## Why

当前审批链路存在两处结构性耦合，已经影响到可维护性与扩展性：

1. **通道实现压在领域引擎层**：`core/feishu_card.py`、`core/feishu_channel.py`、`core/feishu_check.py`、`core/feishu_listen.py` 把 lark-cli 子进程、卡片回调、发件箱轮询与链路自检等**外部传输实现**放在 `core/` 里；`orchestrator.py` 直接持有一个具体通道对象并在挂起分支调用 `first_send`。领域层没有一份稳定的「审批通道契约」，新增任何其它通道都只能照抄飞书实现。
2. **路由与运行时容器强耦合**：`app.py` 单文件承载 53 个应用路由（含 MCP 挂载、SSE 与 SPA 托管），任何接口改动都要在近 800 行的文件里定位；路由无法按业务域独立阅读、复用或测试。

根因不是某条路由写错了，而是**缺少一个显式的通道契约**与**缺少按业务域拆分的路由层**。这次变更只做结构收敛，不改变任何对外行为。

## What Changes

- 新增 `backend/ai_flywheel/core/approval_channel.py`：
  - `ApprovalChannel`（`typing.Protocol`，`runtime_checkable`）：编排器与运行时实际依赖的通道能力——`availability` / `first_send` / `handle_click` / `start` / `stop` / `tick`；
  - 契约级忙碌错误 `ApprovalBusyError`（`ApprovalLockBusy` 成为它的子类），通道据此识别「另一批次持锁」，不再反向导入编排器模块。
- 飞书实现整体迁至 `backend/ai_flywheel/adapters/channels/feishu/`：`card.py`、`channel.py`、`check.py`、`listen.py` 与包入口 `__init__.py`；`backend/ai_flywheel/adapters/channels/__init__.py` 为通道适配层入口。
- `core/feishu_card.py`、`core/feishu_channel.py`、`core/feishu_check.py`、`core/feishu_listen.py` 保留为**薄再导出兼容层**：既有导入路径解析到与适配器层完全相同的对象，测试与外部调用零改动。
- `runtime.py` 从 `adapters.channels.feishu` 导入飞书类，并把 `self.feishu_channel` 标注为 `ApprovalChannel | None`；`orchestrator.py` 的构造参数与属性同样标注为 `ApprovalChannel | None`，**属性名 `feishu_channel` 保持不变**。
- `app.py` 拆分为 `backend/ai_flywheel/routes/` 下四个 `APIRouter` 工厂：
  - `overview.py`：`/health`、`/llms.txt`、`/api/v1/*`、`/api/site/overview|about|seed|connect|config|settings`、`/api/hooks/{client}`；
  - `governance.py`：政策、预检、结果、规则、错题、分歧、成长指标、核心索引卡、复盘、信号、知识库与 Skill 预览；
  - `runs.py`：会话与 `/api/site/runs*`（含后台启动与后台审批）；
  - `automations.py`：`/api/site/automations*`、`/api/site/dream*`、`/api/site/feishu-check`。
  - `app.py` 只保留 `create_app`、lifespan、MCP Streamable HTTP 挂载、CORS、SSE `/api/events`、路由挂载与 SPA 静态托管，目标 ≤ 220 行。
- 新增 `tests/test_approval_channel_abstraction.py`：契约一致性、旧/新导入同源、路由表逐条固化。

## Capabilities

### Modified Capabilities
- `approval-card-channels`：新增「审批通道契约」「通道实现的存放与兼容导入」「门户 API 契约不变」「通道忙时的契约化反馈」四条增量要求；飞书的发送形态、重试、回写与安全默认均不变。

## Impact

- 后端新增：`core/approval_channel.py`、`adapters/channels/__init__.py`、`adapters/channels/feishu/{__init__,card,channel,check,listen}.py`、`routes/{__init__,overview,governance,runs,automations}.py`。
- 后端修改：`core/orchestrator.py`（仅类型标注与忙碌错误基类）、`runtime.py`（导入路径、属性标注、启动审计取 `lark_runner`）、`core/feishu_*.py`（改为兼容再导出）、`app.py`（瘦身为装配层）。
- 测试：新增 `tests/test_approval_channel_abstraction.py`；`tests/test_feishu_group1.py` 的死分支检查路径指向新模块。
- 文档：`AGENTS.md` 包地图同步（`core/` 不再承载 `feishu_*` 实现，新增 `adapters/channels/` 与 `routes/`）。
- 不改变 P00–P07 阶段副作用边界、安全阀、飞书卡片行为、门户 URL / 方法 / 状态码 / 响应结构与前端契约。

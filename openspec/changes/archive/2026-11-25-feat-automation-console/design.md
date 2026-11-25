# Design: 自动化任务控制台与热重载调度器

## 决策 1：将 APScheduler 从 `app.py` 局部变量收敛到 `core/automation.py`

- **备选方案 A**：继续把 `scheduler` 放在 `create_app()` 闭包内，在 `app.py` 路由里通过 `nonlocal` 操作。
  - 缺点：`FlywheelRuntime.update_runtime_config()` 无法感知和热重载调度器，`PUT /api/site/config` 与 `PUT /api/site/automations/schedule` 会出现双轨不一致；单元测试难以直接验证调度器状态。
- **采用方案 B**：新建 `backend/ai_flywheel/core/automation.py` 的 `AutomationManager`，由 `FlywheelRuntime` 持有，在 `serve` 角色 lifespan 启动/停止。
  - 优点：单一事实源；无论是 `PUT /api/site/automations/schedule` 还是 `PUT /api/site/config`，最终都经 `rt.update_runtime_config()` 落盘后调用 `rt.automation.sync_from_config()` 完成热重载；且严格遵守「后台守护线程仅在 `process_role == "serve"` 下运行」的仓库护栏。

## 决策 2：统一使用 `Asia/Shanghai` 时区解析 Cron

- `AGENTS.md` §4 规定所有面向产品的日期统一采用上海时间。此前 `CronTrigger.from_crontab(daily_cron)` 未指定时区，依赖宿主时区。
- `AutomationManager` 复用 `core/time_utils.SHANGHAI_TZ` 构造 `BackgroundScheduler(timezone=SHANGHAI_TZ)` 与 `CronTrigger.from_crontab(expr, timezone=SHANGHAI_TZ)`，避免跨时区部署或容器宿主机 UTC 时导致晚 8 点漂移。

## 决策 3：Cron 校验与人类可读格式化

- 仅接受标准 5 段 crontab（`分 时 日 月 周`），拒绝 6/7 段或越界值。
- `describe_cron(expr)` 对常见模式输出直观中文（如 `0 20 * * *` → `每天 20:00（上海时间）`、`30 21 * * 1-5` → `工作日 21:30（上海时间）`、`0 20 * * 5` → `每周五 20:00（上海时间）`），其余模式回退 `Cron: <expr>（上海时间）`。
- 即使调度器处于暂停状态（`schedule_enabled=false`），预览接口仍基于 `CronTrigger.get_next_fire_time` 计算若开启后的下一次预计触发时间（`preview_next_run_at`），而运行中的真实计划触发时间写入 `next_run_at`。

## 决策 4：前端导航与视觉体系

- 保持 `ai-flywheel` 现有暖纸白 / 墨色 / 青色强调（`index.css` design tokens）的一致视觉，采用参考截图的信息层级：
  - 顶部标题 + 胶囊分区锚点栏 + 回到顶部；
  - 「后台调度器」总控卡（Toggle + 状态横条 + 立即巡检按钮）；
  - 「每日与周期任务」主卡（一键执行每日沉淀 + 可视化时间选择器与 Cron 输入联动 + 周五周度复盘勾选框 + 2 列大号 KPI 卡片）；
  - 「后台守护巡检」2 列守护状态卡（Recovery Watchdog + Feishu Channel）；
  - 「自动化执行记录」列表。
- 左侧导航新增 `automations`（「自动化任务」，时钟图标），移动端底部栏同步调整为 5 列（`grid-cols-5`）。

## 验证策略

- **单元测试（`test`）**：`tests/test_automation_console.py` 覆盖 `AutomationManager` 的 `serve` vs `cli` 角色隔离、上海时区 Cron 校验/拒绝、热启停与热改 Cron 刷新 `next_run_at`、手动 `POST /api/site/automations/recover` 收割陈旧批次。
- **真实进程与仿真验证（`real`）**：扩展隔离端到端仿真套件，真实启动 `flywheel serve`，验证 `GET /api/site/automations`、`PUT /api/site/automations/schedule` 热修改 Cron（`0 20 * * *` → `30 21 * * *` → 还原）、非法 Cron 返回 400、关闭自动调度后手动触发仍成功、以及 `POST /api/site/automations/recover` 实时收割陈旧批次；并通过 `python scripts/verify_repo.py --frontend` 4/4 全绿验收。

# Proposal: 自动化任务可视化控制台与定时调度热管理

## Why

当前 `ai-flywheel` 已在服务端内置了每日定时沉淀调度器（APScheduler `daily_cron`）、周五周度 Dream 复盘、30 秒中断批次自动收割线程（`flywheel-recovery`）以及飞书发件箱同步/点击监听，但这些后台自动化任务完全不可见、不可控：

1. **无可视化入口**：Web 控制台仅展示「每日成果 / 每日沉淀 / 规则与规矩 / 接入与设置 / 关于」，用户无法在一个页面看到当前定时任务是否在运行、当前 Cron 规则是什么、上次何时执行、下次何时触发以及守护巡检是否健康。
2. **调度配置无法热更新**：`app.py` 将 `APScheduler` 作成 `create_app` 内部局部变量，修改 `workflow.schedule_enabled` 或 `workflow.daily_cron` 必须重启 `flywheel serve` 才能生效，且未显式锁定上海时区（`Asia/Shanghai`），与全仓统一上海日期契约存在漂移风险。
3. **缺少统一手动巡检与触发面**：定时沉淀、周度复盘与中断批次收割分散在不同入口或无界面按钮，难以排查或临时补跑。

## What Changes

- 新增后端 `core/automation.py`（`AutomationManager`），由 `FlywheelRuntime` 统一持有并管理 `APScheduler`（强制 `Asia/Shanghai` 时区）及中断恢复巡检统计；支持 `schedule_enabled`、`daily_cron`、`friday_weekly_enabled` 的**热重载（无需重启服务）**。
- 在 `app.py` 暴露 `/api/site/automations`（查询调度器状态、上次/下次触发时间、今日与累计统计、守护线程健康及定时执行历史）、`PUT /api/site/automations/schedule`（校验 5 段 Cron、落盘并实时重载调度器）、`POST /api/site/automations/recover`（手动立即执行一轮中断批次收割巡检）。
- 将 `workflow.schedule_enabled` 与 `workflow.daily_cron` 从 `FlywheelRuntime.STARTUP_ONLY_PATHS` 移除，并在 `update_runtime_config` 落盘后自动同步热重载 `AutomationManager`。
- 前端左侧导航新增「**自动化任务**」（路由 `/automations`），提供「后台调度器（总开关 + 实时状态条 + 立即巡检）」「每日与周期任务（可视化 Cron 时间编辑 + 周五周度复盘开关 + 今日/累计 KPI 卡 + 一键手动触发）」「后台守护巡检（中断收割 & 飞书通道）」「自动化执行记录」四个分区。

## Capabilities

### New Capabilities
- `automation-console`: 后台定时调度器的可观测状态、上海时区 Cron 校验与热重载、手动巡检收割接口及前端「自动化任务」可视化控制面。

### Modified Capabilities
- `workflow-orchestration`: `workflow.schedule_enabled` 与 `workflow.daily_cron` 变更由“需重启生效”升级为“运行时热重载生效”，且定时任务触发时间统一按 `Asia/Shanghai` 时区计算。

## Impact

- 后端：`backend/ai_flywheel/core/automation.py`（新增）、`backend/ai_flywheel/runtime.py`、`backend/ai_flywheel/app.py`。
- 前端：`frontend/src/pages/AutomationsPage.tsx`（新增）、`frontend/src/App.tsx`、`frontend/src/api.ts`、`frontend/src/types.ts`。
- 测试与文档：`tests/test_automation_console.py`（新增）、`tests/test_wave2b_service_layer.py`（同步热重载契约）、`AGENTS.md`、`README.md`、`CHANGELOG.md`。
- 不改变 P00–P07 阶段副作用边界与安全阀契约；关闭自动调度后仅停自动触发，手动执行按钮仍可用。

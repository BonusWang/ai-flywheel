# Tasks: feat-automation-console

- [x] 1. 实现 `backend/ai_flywheel/core/automation.py`（`AutomationManager`、上海时区 `CronTrigger` 校验与中文描述、热启停与重载、中断收割状态追踪、状态快照聚合）【test】
- [x] 2. 在 `runtime.py` 与 `app.py` 接入 `AutomationManager`：将 `workflow.schedule_enabled` / `workflow.daily_cron` 升级为热重载，暴露 `GET /api/site/automations`、`PUT /api/site/automations/schedule`、`POST /api/site/automations/recover`，并在 `serve` lifespan 中托管启停与恢复统计【test】
- [x] 3. 在 `frontend/src/` 新增 `pages/AutomationsPage.tsx`，对接 `types.ts`、`api.ts` 与 `App.tsx` 左侧导航（`/automations`），完成后台调度器、可视化 Cron 编辑、周期任务 KPI、守护巡检与执行记录视图，构建通过 `tsc -b && vite build`【test】
- [x] 4. 新增 `tests/test_automation_console.py` 并更新 `tests/test_wave2b_service_layer.py`，同步 `AGENTS.md`、`README.md`、`CHANGELOG.md`【test】
- [x] 5. 执行 `python scripts/verify_repo.py --frontend` 与隔离真实进程端到端仿真验证（覆盖 `/api/site/automations` 热改 Cron、非法 Cron 400、停调度后手动触发与手动巡检收割）【real】

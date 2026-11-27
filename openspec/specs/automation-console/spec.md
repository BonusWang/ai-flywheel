# automation-console Specification

## Purpose
TBD - created by archiving change feat-automation-console. Update Purpose after archive.
## Requirements
### Requirement: 定时调度器状态可观测
系统 SHALL 通过 `GET /api/site/automations` 暴露后台定时调度器与守护任务的实时状态，包括：
- 定时调度器是否启用（`schedule_enabled`）、调度线程是否在运行（`scheduler_running`）、当前 `daily_cron` 表达式、人类可读中文时间说明与时区（`Asia/Shanghai`）；
- `friday_weekly_enabled` 开关状态、当前有效审批模式与运行器模式；
- 上次定时或自动执行时间（`last_run_at`）、下一次计划触发时间（`next_run_at`，按 `Asia/Shanghai` 计算）；
- 今日沉淀批计数/状态、待审会话数、待审批批次数与累计定时执行统计；
- 中断批次收割守护（`recovery_watchdog`）与飞书通道守护（`feishu_daemon`）的运行状态；
- 最近自动化/定时执行记录列表。

#### Scenario: 读取自动化任务状态快照
- **WHEN** 客户端请求 `GET /api/site/automations`
- **THEN** 返回 HTTP 200 及完整的调度器状态、`next_run_at`（含 `+08:00` 偏移）、守护巡检状态、KPI 统计与最近执行记录

### Requirement: 定时调度配置校验与热重载
系统 SHALL 通过 `PUT /api/site/automations/schedule`（以及 `PUT /api/site/config` 修改 `workflow.schedule_enabled` / `workflow.daily_cron` / `workflow.friday_weekly_enabled` 时）支持运行时热更新定时调度配置，无需重启 `flywheel serve`：
- 修改 `daily_cron` 前 MUST 校验其为合法的 5 段 crontab 表达式；非法表达式 MUST 拒绝并返回 HTTP 400，且不得改写配置文件或破坏现有已注册任务；
- 将 `schedule_enabled` 切为 `false` 时 MUST 立即移除或暂停自动定时任务，关闭后不再自动触发，但手动触发接口仍可正常使用；
- 将 `schedule_enabled` 切为 `true` 或修改 `daily_cron` 时 MUST 立即在运行中的调度器上注册或替换 `daily_flywheel` 任务并刷新 `next_run_at`；
- 非 `serve` 角色进程下修改配置 SHALL 正常校验并落盘，但 MUST NOT 启动后台定时调度线程（遵守背景守护仅在 `serve` 角色运行的护栏）。

#### Scenario: 可视化修改 Cron 实时热生效
- **WHEN** `serve` 角色下通过 `PUT /api/site/automations/schedule` 将 `daily_cron` 从 `0 20 * * *` 修改为 `30 21 * * *`
- **THEN** 返回 HTTP 200、`restart_required` 为假、配置文件已持久化且 `next_run_at` 立即反映次日或当日上海时间 `21:30`

#### Scenario: 非法 Cron 表达式被拒绝
- **WHEN** 客户端向 `PUT /api/site/automations/schedule` 提交非法 Cron（如 `99 99 * * *` 或 `not-a-cron`）
- **THEN** 返回 HTTP 400 且原有 `daily_cron` 与运行中的定时任务保持不变

#### Scenario: 关闭自动调度后仍允许手动触发
- **WHEN** 客户端将 `schedule_enabled` 设为 `false` 后再调用 `POST /api/site/runs`
- **THEN** 调度器状态变为已暂停且无活跃定时任务，但手动触发批次仍按正常流程执行

### Requirement: 手动触发中断批次收割巡检
系统 SHALL 提供 `POST /api/site/automations/recover` 接口，允许运维者在自动化任务界面一键立即执行一轮中断批次收割（`recover_interrupted_runs`），更新守护巡检的 `last_pass_at` 与 `last_reaped_count`，并返回本次收割结果与最新自动化状态快照。

#### Scenario: 一键巡检收割陈旧运行批次
- **WHEN** 存在心跳超时的 `RUNNING` 批次时调用 `POST /api/site/automations/recover`
- **THEN** 该批次被收割为 `FAILED`、独占锁释放、会话回到 `pending`，接口返回 `reaped_count >= 1` 与更新后的守护巡检时间戳

### Requirement: 前端自动化任务控制台页面
Web 控制台 SHALL 在左侧导航提供独立入口「自动化任务」（路由 `/automations`），并包含以下分区：
- 顶部标题与分区导航胶囊（后台调度器、定时与周期任务、后台守护巡检、执行记录）及回到顶部操作；
- 后台调度器卡片：总开关 Toggle、实时运行状态横条（含上次/下次触发时间）及「立即巡检收割」按钮；
- 每日与周期任务卡片：支持可视化修改触发时间（预设频率 + 时/分选择 + 5 段 Cron 表达式实时联动与保存）、周五周度复盘开关、今日/累计 KPI 统计卡以及一键手动执行每日/周度沉淀按钮；
- 后台守护巡检卡片与自动化执行记录表，支持点击跳转至「每日沉淀」查看批次详情。

#### Scenario: 控制台展示并热修改定时任务
- **WHEN** 用户打开 `/automations` 页面并切换自动执行开关或修改触发时间后保存
- **THEN** 页面调用自动化接口完成热重载并实时刷新状态条与下一次触发时间

### Requirement: 定时调度防休眠静默漏跑与可观测补跑
系统 SHALL 避免因宿主机短暂休眠或进程阻塞导致每日定时沉淀静默丢失，并暴露调度漏跑与进程启动时间的可观测信号：
- `WorkflowConfig` MUST 提供 `misfire_grace_seconds`（默认 `14400`，范围 ≥ 60）与 `catchup_missed_schedule`（默认 `true`），并在 `flywheel.yaml.example` 中提供对应注释与默认值；APScheduler 注册 `daily_flywheel` 任务时 MUST 使用 `misfire_grace_seconds` 而非硬编码 300 秒；
- `serve` 角色在启动完成与 `recovery` 周期巡检时 MUST 检查：当 `schedule_enabled = true`、`workflow_enabled = true`、当前上海时间已超过当日 `daily_cron` 最近一次应触发点（且处于 `misfire_grace_seconds` 补跑窗口内）、`today_scheduled_ran = false` 且存在待处理队列/待审会话时，记录一条 `schedule_misfire_detected` 审计事件（每日最多一次）；若 `catchup_missed_schedule = true` 且当前无独占锁占用，MUST 自动触发一次 `trigger = "scheduled"` 补跑；
- 非 `serve` 角色进程下 MUST NOT 执行自动补跑；
- `GET /health` 与 `GET /api/site/automations` MUST 暴露服务进程启动时间 `started_at`（ISO 8601）；`GET /api/site/automations` 的 `scheduler` 段 MUST 暴露 `misfire_grace_seconds`、`catchup_missed_schedule` 与 `missed_today`（当日是否已过应触发点但尚未运行）。

#### Scenario: 宿主机休眠跨过 20:00 后在宽限窗口内自动检测并补跑
- **WHEN** `serve` 角色配置的 `daily_cron = "0 20 * * *"`、`misfire_grace_seconds = 14400`、`catchup_missed_schedule = true`，当前上海时间为当日 `21:15`、`today_scheduled_ran = false` 且存在待处理队列项，巡检执行补跑检查
- **THEN** 系统必须记录一条 `schedule_misfire_detected` 审计事件，并触发一次 `trigger = "scheduled"` 批次
- **AND** 当日后续巡检因 `today_scheduled_ran = true` 不得重复补跑

#### Scenario: 关闭自动补跑时仅记录漏跑告警不自动触发
- **WHEN** `catchup_missed_schedule = false` 且当日已过应触发点尚未运行
- **THEN** `GET /api/site/automations` 必须返回 `scheduler.missed_today = true`，记录 `schedule_misfire_detected` 审计事件，且不得自动启动批次

#### Scenario: 健康检查与自动化快照返回服务启动时间
- **WHEN** 客户端请求 `GET /health` 或 `GET /api/site/automations`
- **THEN** 响应必须包含非空 `started_at` 字段，反映当前运行中服务进程的启动时间戳

### Requirement: 前端自动化控制台展示进程启动时间、补跑配置与漏跑状态
Web 控制台 `/automations` 页面与前端类型定义（`frontend/src/types.ts`、`frontend/src/pages/AutomationsPage.tsx`）SHALL 与后端 `GET /api/site/automations` 新增的调度可观测字段保持对齐：
- `AutomationsSnapshot.scheduler` 类型 MUST 包含 `started_at`、`misfire_grace_seconds`、`catchup_missed_schedule`、`missed_today`；
- 后台调度器状态区 MUST 展示服务进程启动时间与补跑宽限窗口摘要，并在 `missed_today == true` 时给出醒目的当日漏跑提醒标识。

#### Scenario: 控制台正确渲染漏跑提醒与进程启动时间
- **WHEN** `GET /api/site/automations` 返回 `scheduler.started_at` 非空且 `scheduler.missed_today = true`
- **THEN** 前端构建通过类型校验，且 `/automations` 页面渲染进程启动时间与当日漏跑提示状态


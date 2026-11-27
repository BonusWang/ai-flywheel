# automation-console 变更规格

## ADDED Requirements

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

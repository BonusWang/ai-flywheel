# automation-console 变更规格

## ADDED Requirements

### Requirement: 前端自动化控制台展示进程启动时间、补跑配置与漏跑状态
Web 控制台 `/automations` 页面与前端类型定义（`frontend/src/types.ts`、`frontend/src/pages/AutomationsPage.tsx`）SHALL 与后端 `GET /api/site/automations` 新增的调度可观测字段保持对齐：
- `AutomationsSnapshot.scheduler` 类型 MUST 包含 `started_at`、`misfire_grace_seconds`、`catchup_missed_schedule`、`missed_today`；
- 后台调度器状态区 MUST 展示服务进程启动时间与补跑宽限窗口摘要，并在 `missed_today == true` 时给出醒目的当日漏跑提醒标识。

#### Scenario: 控制台正确渲染漏跑提醒与进程启动时间
- **WHEN** `GET /api/site/automations` 返回 `scheduler.started_at` 非空且 `scheduler.missed_today = true`
- **THEN** 前端构建通过类型校验，且 `/automations` 页面渲染进程启动时间与当日漏跑提示状态

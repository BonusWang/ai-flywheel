# Proposal: 治理与进化控制面 Web 控制台升级（P0-1 & P0-2）

## Why

`AI Flywheel` 的核心产品定位已明确为**面向多 AI 编码工具的本地优先「治理与进化控制面」**（执行纪律与实证门禁 + 抗熵增进化引擎 + 受控审批流水线），与 OpenViking / Mem0 等上下文数据库/记忆引擎形成上下游正交互补。然而，当前 Web 控制台与接口暴露存在三处明显错位：

1. **首页与导航门面错位**：左侧导航前两项为「每日成果 / 每日沉淀」，首页（`HomePage.tsx`）约 70% 版面用于展示笔记列表，外观像“日常笔记阅读器”；同时首页第 125 行硬编码了 `每天 20:00（周五加周复盘）`，与 v0.5 热重载调度器的真实运行态脱节。
2. **后端硬核治理资产未在前端透出**：后端已稳定计算并存储了实证通过率（`real_verification_rate`）、证据验收台账（`GET /api/site/outcomes`）、Skill 实效账本（`skill_assets`）、经验卡新鲜度（`knowledge_card_metrics`）、跨端核心索引卡（`GET /api/site/core-card`）、P00/P02 噪音过滤与模型遥测（`triage_report` / `call_telemetry`）以及周五分层蒸馏（`core/dream.py`），但控制台缺少对应视图和触发入口。
3. **跨页配置重复**：`ConnectPolicyPage` 与 `AutomationsPage` 重复暴露 `schedule_enabled` / `friday_weekly_enabled` 调度开关，增加了理解成本。

## What Changes

- **后端轻量读接口与蒸馏预览/触发（不触碰热文件）**：
  - `core/dream.py`：新增只读 `preview_dream_consolidation()`（零副作用预览待休眠规则、超期经验卡、规则合并候选、Skill 实效告警与待收敛分歧）。
  - `app.py`：
    - 新增 `GET /api/site/skills` 暴露 Skill 实效账本与本地探测数；
    - 增强 `GET /api/site/knowledge` 为每篇文档合并新鲜度与助益字段（`freshness_tier`、`valid_until`、`is_expired`、`freshness_state`、`surface_count`、`helped_count`，完全向后兼容）；
    - 新增 `GET /api/site/dream`（最近蒸馏审计摘要 + 当前只读预览）与 `POST /api/site/dream/run`（支持 `dry_run=true` 只读预览或 `dry_run=false` 立即执行蒸馏并刷新核心索引卡）。
- **前端 `5+1` 控制面信息架构与首页重塑（P0-1）**：
  - 保留 `5+1` 极简骨架，将一级导航升级为：**今日态势**（`/`）、**沉淀与审批**（`/runs`）、**自动化调度**（`/automations`）、**规则与进化**（`/rules`）、**接入与设置**（`/settings`）+ **关于**（`/about`）。
  - 重构 `HomePage.tsx` 为「今日态势」治理指挥舱：顶部展示 4 张治理 KPI（真实证据通过率 & 预检激活、规则健康度、错题观察池 & 自查率、调度与安全阀实况——读取真实 `/api/site/automations` 与 `/api/site/growth-metrics` 彻底消除硬编码）；中部优先展示待审批/待收敛预警、最近一次 P00–P07 摘要与核心索引卡快捷入口；底部收敛展示带新鲜度标签的受控写入资产。
  - 收口 `ConnectPolicyPage.tsx`：移除与 `AutomationsPage` 重复的调度开关，改为状态摘要与跳转链接，并将安全阀配置置顶醒目展示。
- **治理资产、噪音漏斗、遥测与蒸馏视图补齐（P0-2）**：
  - `RulesRetroPage.tsx`：补齐「Skill 与经验保鲜」「证据台账（Outcomes）」Tab 与页头「核心索引卡（`FLYWHEEL-CORE.md`）」弹窗预览 + 一键刷新。
  - `WorkflowsPage.tsx`：展开批次时展示 P00→P02→P03「噪音过滤漏斗条」，在节点详情页透出模型调用遥测徽章（模型名、耗时、Token 与缓存命中）。
  - `AutomationsPage.tsx`：在「每日与周期任务」区域新增「周五分层蒸馏控制卡」，支持查看最近蒸馏摘要、一键只读预览建议与手动触发执行。

## Capabilities

### New Capabilities
- `control-plane-console`: 治理与进化控制面 REST 读/蒸馏接口（Skill 实效账本、知识新鲜度融合、蒸馏预览与手动触发）及前端控制面指挥舱、治理资产视图、噪音漏斗与遥测展示契约。

### Modified Capabilities
- 无（保持既有 P00–P07 流水线、自动化热调度与飞书交互审批全部契约不变）。

## Impact

- 后端：`backend/ai_flywheel/core/dream.py`、`backend/ai_flywheel/app.py`。
- 前端：`frontend/src/App.tsx`、`frontend/src/api.ts`、`frontend/src/types.ts`、`frontend/src/pages/HomePage.tsx`、`frontend/src/pages/RulesRetroPage.tsx`、`frontend/src/pages/WorkflowsPage.tsx`、`frontend/src/pages/AutomationsPage.tsx`、`frontend/src/pages/ConnectPolicyPage.tsx`。
- 测试：`tests/test_control_plane_console.py`（新增）。
- 不触碰 `core/orchestrator.py`、`core/feishu_*`、`adapters/runners/*`、`storage/db/*` 等热文件，对已有配置与 API 100% 向后兼容。

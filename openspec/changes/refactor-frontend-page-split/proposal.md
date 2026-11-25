# Proposal: 前端页面组件化拆分与「关于」页定位对齐（P1-1）

## Why

**现象**：`frontend/src/pages/WorkflowsPage.tsx` 已达 1,103 行、`frontend/src/pages/RulesRetroPage.tsx` 已达 1,084 行（`wc -l` 实测）。两个文件同时承载数据加载、状态编排、十余个交互区块与全部 JSX 渲染，任何一次小改动都要在千行文件里定位；审批抽屉、节点详情、规则卡片、策略表单等区块无法被其他页面复用。

**影响**：
1. 可维护性：区块之间只靠页面级状态耦合，改动一处需要通读全文件；多人/多 Agent 并行修改同一页面时冲突概率高。
2. 一致性：核心索引卡弹窗（`FLYWHEEL-CORE.md` 预览与重建）在 `HomePage.tsx` 与 `RulesRetroPage.tsx` 中各实现了一份，行为已经开始漂移（一处展示来源字段、一处不展示）。
3. 产品定位：`AboutPage.tsx` 的英雄文案与页脚架构说明仍是旧叙事，未体现 README 已确立的「面向多 AI 编码工具的本地优先『治理与进化控制面』」定位与「与 OpenViking / Mem0 等上下文数据库正交互补」的边界；页面跳转按钮仍写「查看每日沉淀」，与控制台一级导航「沉淀与审批」不一致（`AutomationsPage.tsx` 也有一处同源旧页面名文案）。

**根因层级**：不是某个区块写错，而是页面文件缺少组件边界——展示组件与页面编排逻辑混在一起，复用只能靠复制。因此本次以「拆分为职责单一的组件 + 页面只做编排」治本，而不是继续在千行文件里打补丁。

## What Changes

- **「沉淀与审批」页面组件化**：把 `WorkflowsPage.tsx` 拆为 `components/workflows/` 下的 `SkillUpdatePanel`、`RunStatusPill`（含 `StageDot`）、`ApprovalDrawer`、`NodeDetailPanel`、`ManualSessionModal`、`NoiseFunnelStrip`、`RunPipelineRow`、`RunDetailPanel`；页面容器只保留数据加载、审批选择 `initCount` 重建、SSE 刷新与事件处理，行数压到 400 行以内。
- **「规则与进化」页面组件化**：把 `RulesRetroPage.tsx` 拆为 `components/rules/` 下的 `RulesListTab`、`CluesObservationsTab`、`AssetsFreshnessTab`、`OutcomesLedgerTab`、`PolicyStudioTab`、`CoreCardModal`；页面容器只保留聚合加载、页签切换与全局提示，行数压到 300 行以内。
- **核心索引卡弹窗复用**：`CoreCardModal` 同时用于 `RulesRetroPage` 与 `HomePage`，消除两份重复实现。
- **「关于」页定位对齐**：英雄文案、四阶段说明与页脚架构说明对齐 README 的「治理与进化控制面」定位（执行纪律与实证门禁 + 抗熵增进化引擎 + 受控审批流水线，与 OpenViking / Mem0 等上下文数据库正交互补）；把「查看每日沉淀」改为「查看沉淀与审批」，并同步 `AutomationsPage.tsx` 中一处旧页面名文案。

**行为契约不变**：噪音过滤漏斗、审批选择按计划写入数量重建（`initCount`）、SSE 刷新、飞书审批状态文案、`stages`/`digest`/`sessions`/`events` 四个详情页签、五个治理页签、规则筛选与状态迁移、分歧新增/吸收/忽略、错题升级/归档、实时线索处理、策略保存/恢复默认、核心索引卡预览与重建全部保持可用；不触碰后端、API、数据库与 P00–P07 流水线。

## Capabilities

### New Capabilities

无（本次不新增能力，只重组前端实现与对齐文案）。

### Modified Capabilities

- `control-plane-console`：新增「前端页面模块化拆分与行为保持」与「关于页产品定位与导航文案一致性」两条需求。该能力的基线正由在途变更 `feat-control-plane-console-views` 建立，本变更在其之上补充增量，不重复声明其既有需求。

## Impact

- 前端新增：`frontend/src/components/workflows/`（8 个组件）、`frontend/src/components/rules/`（6 个组件）。
- 前端修改：`frontend/src/pages/WorkflowsPage.tsx`、`frontend/src/pages/RulesRetroPage.tsx`、`frontend/src/pages/AboutPage.tsx`、`frontend/src/pages/HomePage.tsx`（改用共享 `CoreCardModal`）、`frontend/src/pages/AutomationsPage.tsx`（仅两处旧页面名文案）。
- 规格：`openspec/changes/refactor-frontend-page-split/`。
- 不触碰后端、`core/orchestrator.py`、`core/feishu_*`、`adapters/runners/*`、`storage/db/*` 等热文件；无新增配置键、无 API 变更。

# Tasks: 前端页面组件化拆分与「关于」页定位对齐

## 1. 「沉淀与审批」页面组件化
- [x] 1.1 新增 `frontend/src/components/workflows/SkillUpdatePanel.tsx` 与 `RunStatusPill.tsx`（含 `StageDot`），从 `WorkflowsPage.tsx` 原样迁移展示逻辑 【static】
- [x] 1.2 新增 `frontend/src/components/workflows/ManualSessionModal.tsx` 与 `ApprovalDrawer.tsx`，分别承载手工补录表单与审批抽屉（计划写入勾选、同意/取消、飞书状态行） 【static】
- [x] 1.3 新增 `frontend/src/components/workflows/NodeDetailPanel.tsx`、`NoiseFunnelStrip.tsx`、`RunPipelineRow.tsx`、`RunDetailPanel.tsx`，迁移阶段详情、噪音漏斗、批次行与展开详情 【static】
- [x] 1.4 重写 `frontend/src/pages/WorkflowsPage.tsx` 为编排容器：只保留数据加载、`initCount` 审批选择、SSE 刷新与事件处理，行数 < 400 【test】

## 2. 「规则与进化」页面组件化
- [x] 2.1 新增 `frontend/src/components/rules/RulesListTab.tsx`，迁移新建规则表单、规则卡片、状态迁移与分歧新增/吸收/忽略 【static】
- [x] 2.2 新增 `frontend/src/components/rules/CluesObservationsTab.tsx`、`AssetsFreshnessTab.tsx`、`OutcomesLedgerTab.tsx`，迁移错题与线索、Skill 与保鲜、证据台账视图 【static】
- [x] 2.3 新增 `frontend/src/components/rules/PolicyStudioTab.tsx`，迁移三份策略表单与保存/恢复默认动作，并通过 `ref` 向页头暴露动作 【static】
- [x] 2.4 新增 `frontend/src/components/rules/CoreCardModal.tsx`，并在 `RulesRetroPage.tsx` 与 `HomePage.tsx` 中共用 【static】
- [x] 2.5 重写 `frontend/src/pages/RulesRetroPage.tsx` 为编排容器：只保留聚合加载、页签切换与全局提示，行数 < 300 【test】

## 3. 「关于」页定位与全站文案对齐
- [x] 3.1 更新 `frontend/src/pages/AboutPage.tsx`：英雄文案、四阶段说明与页脚架构说明对齐「治理与进化控制面」定位及与上下文数据库的正交互补边界 【static】
- [x] 3.2 将 `AboutPage.tsx` 的「查看每日沉淀」改为「查看沉淀与审批」，并同步 `frontend/src/pages/AutomationsPage.tsx` 一处旧页面名文案 【static】

## 4. 验收
- [x] 4.1 `cd frontend && npm run build` 退出码 0、零 TypeScript 错误 【test】
- [x] 4.2 `openspec validate --all --strict` 全绿 【test】
- [x] 4.3 `wc -l` 确认 `WorkflowsPage.tsx` < 400、`RulesRetroPage.tsx` < 300，并逐区块核对行为保持清单 【static】

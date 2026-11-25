# Design: 前端页面组件化拆分与「关于」页定位对齐

## 上下文与设计约束

- 本次是纯前端重构 + 文案对齐：不修改后端、API 契约、数据库与 P00–P07 流水线，也不引入新配置键。
- 页面行为必须逐项保持：审批选择 `initCount` 重建、噪音过滤漏斗、模型遥测、P05 蒸馏摘要、四个详情页签、五个治理页签、所有写操作与提示文案。
- 约束：`WorkflowsPage.tsx` < 400 行、`RulesRetroPage.tsx` < 300 行；`npm run build` 零 TypeScript 错误；`openspec validate --all --strict` 全绿。
- 与在途变更 `feat-control-plane-console-views` 共用工作区：只重组它刚建立的页面内容，不回退其接口与视图；`control-plane-console` 能力基线由该变更建立，本变更只补增量需求。

## 关键设计决策

### 1. 「沉淀与审批」页的组件边界

**方案 A（采用）**：页面保留全部状态（runs / sessions / 展开行 / 选中节点 / 详情页签 / 审批选择 / 飞书状态 / busy / error）与数据加载、事件处理；组件只接收 props 并渲染。
- `RunStatusPill` + `StageDot`：纯展示。
- `SkillUpdatePanel`：自持「读取现有 Skill 预览 / 展开全文」局部状态，输入是一个 `PlannedWrite`。
- `ApprovalDrawer`：计划写入勾选清单 + 常规项/同意/取消动作 + 飞书通道状态行；索引切换与提交仍由页面处理，`data-testid="feishu-link-status"` 保持不动。
- `NodeDetailPanel`：阶段列表 + 选中阶段详情（摘要、错误、产出文件、自动检查、`call_telemetry`、P05 Dream 摘要）。
- `NoiseFunnelStrip`：P00 初筛 / P02 语义闸门 / 计划写入数量。
- `RunPipelineRow`：单行批次（状态、日期与批次号、八阶段连线、耗时、审批/查看按钮），展开时渲染 `RunDetailPanel`。
- `RunDetailPanel`：审批抽屉、漏斗、详情页签与四个页签内容（日报 / 对话 / 审计）。

**方案 B（放弃）**：只拆任务清单里点名的五个组件，页面其余 JSX 原样保留。实测剩余约 630 行，无法满足 < 400 行约束，且「行内审批 + 行内详情」仍与页面状态强耦合。
**取舍**：方案 A 多出 `RunPipelineRow` / `RunDetailPanel` / `NoiseFunnelStrip` 三个薄组件，但页面容器真正退化为编排层；状态仍单点持有，避免把 `initCount`、展开行与页签状态分散到多个组件后出现不同步。

### 2. 「规则与进化」页的组件边界与动作注册

**方案 A（采用）**：页面持有聚合数据与页签状态，五个 Tab 组件各自持有本视图的编辑/交互状态，并在操作成功后调用页面传入的 `onChanged`（重新拉取全部数据 + 全局刷新）与 `onToast`。
- `RulesListTab`：新建规则表单、规则卡片、状态迁移、分歧新增/吸收/忽略；筛选结果与「新增一条规则」按钮仍由页头控制（`showCreate` 由页面持有）。
- `CluesObservationsTab`：错题观察池与实时异常线索。
- `AssetsFreshnessTab` / `OutcomesLedgerTab`：只读账本。
- `PolicyStudioTab`：策略表单、保存/恢复默认；通过 React 19 的 `ref` 属性向页面暴露 `save()` / `reset()`，使页头「保存修改 / 恢复默认」按钮位置与行为保持不变（`useImperativeHandle`，无需 `forwardRef`）。
- `CoreCardModal`：弹窗自己调用 `api.refreshCoreCard()`，通过 `onRebuilt` 把新卡片交回页面；`HomePage` 以同一组件替换其内联实现。

**方案 B（放弃）**：把所有编辑状态提升到页面。页面会重新膨胀到 320+ 行并超出 300 行约束；且策略表单字段与页面加载逻辑无关，提升只会增加耦合。
**取舍**：Tab 组件直接调用 `api` 并在成功后通知页面刷新，与仓库现有「页面调用 api + loadAll」模式一致；页面仍是数据与刷新的唯一入口。

### 3. 「关于」页文案对齐

- 英雄段落加入「面向多 AI 编码工具的本地优先『治理与进化控制面』」定位与三项构成（执行纪律与实证门禁、抗熵增进化引擎、受控审批流水线），并说明与 OpenViking / Mem0 等上下文数据库正交互补。
- 四阶段说明改为强调「开工预检 + 完工证据门禁」「错题观察池事不过三」「分歧不覆盖 + 新鲜度衰减 + 周五蒸馏」「P02/P03 只读、P04 唯一受控写入」。
- 页脚架构说明保留 3+1 分层与脱敏端点表述，前置一句控制面定位与互补边界。
- 「查看每日沉淀」改为「查看沉淀与审批」；同步 `AutomationsPage.tsx` 两处旧页面名，保证全站文案一致。

### 4. 行为保持的具体锚点

| 行为 | 拆分后的归属 | 保持方式 |
|---|---|---|
| 审批选择按计划写入数重建（`initCount`） | 页面 `loadData` + `approvalSel` | 原逻辑逐行保留，未下放到组件 |
| SSE / refreshToken 刷新 | 页面 `useEffect` | 未改动 |
| 飞书审批状态文案 | 页面 effect + `feishuLabel` prop | 状态映射逐行保留 |
| 展开行默认节点与页签 | 页面 `toggleRow` | 未改动 |
| 节点详情四类信息 | `NodeDetailPanel` | JSX 与条件逐行迁移 |
| 治理五页签与筛选 | 页面页签 + `RulesListTab` | 筛选仍在页头，规则卡片迁移 |
| 策略保存/恢复默认 | `PolicyStudioTab` | 三个策略接口调用顺序与提示语不变 |
| 核心索引卡重建 | `CoreCardModal` | 两个页面共用，刷新后回写页面状态 |

## 验证计划

1. **构建（test）**：`cd frontend && npm run build` 退出码 0、零 TypeScript 错误。
2. **规格（test）**：`openspec validate --all --strict` 全绿。
3. **规模（static）**：`wc -l` 确认 `WorkflowsPage.tsx` < 400、`RulesRetroPage.tsx` < 300。
4. **行为（static）**：逐区块对照拆分前后的 JSX 与状态映射，确认本文件第 4 节表格中的每一项均有对应实现；不新增/删除任何 `api` 调用。

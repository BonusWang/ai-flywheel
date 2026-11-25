# Proposal: 编排器拆分为 core/pipeline 包，并新增 P04 可选本地 Git 审计提交

## Why

`backend/ai_flywheel/core/orchestrator.py` 已经长到 1845 行，一个文件同时承载五类互不相干的东西：节点常量与错误类型、SSE 事件总线、错题/分歧候选归一化与晋升规则、锁检查点与终态收尾、以及 P00–P07 的阶段序列。由此带来三个具体问题：

1. **阅读与评审成本**：任何一次 P04 改动都要在近两千行里定位；阶段之间的副作用边界靠记忆维持，无法按模块阅读。
2. **改动碰撞**：`AGENTS.md` 已经把该文件列为 hot file。多人/多 agent 同时改不同关注点（例如心跳与知识路由）时冲突面被放大。
3. **缺少可选的写入审计轨迹**：P04 写入并校验通过的文件没有本地版本轨迹。事后想回答「这一批到底改了哪些文件、谁批准的」只能查回执与审计事件，无法从仓库历史直接看到。

根因是**编排器缺少按职责拆分的模块边界**，以及**写入后缺少一个默认关闭、只覆盖本批写入的本地提交动作**。这次变更只做结构收敛，P00–P07 的阶段语义、副作用边界、锁不变量与 fail-run 契约一律不变。

## What Changes

- 新增 `backend/ai_flywheel/core/pipeline/` 包：
  - `constants.py`：`NODE_TEMPLATES`、`PLAN_SOURCE_LABELS`、`REVIEW_SOURCE_LABELS`、`LANDING_FAILED_REASON`、`WorkflowLockError`、`ApprovalLockBusy`、`LockLostError` 与失败调用遥测辅助；
  - `events.py`：`EventBroker`；
  - `helpers.py`：错题/分歧候选归一化、双轨晋升、路由校验、部分写入载荷；
  - `recovery.py`：心跳、`_ensure_lock`、`recover_interrupted_runs`、`_mark_remaining_nodes_skipped`、`_finalize_terminal_artifacts`、`_fail_run`；
  - `coordinator.py`：`WorkflowOrchestrator`（P00–P07 阶段序列、审批认领与续跑）；
  - `git_commit.py`：P04 可选的本地 Git 提交。
- `backend/ai_flywheel/core/orchestrator.py` 改为**薄兼容门面**（48 行）：再导出全部公开符号，并作为协作者（`build_runner` / `run_dream_consolidation` / `rebuild_core_card`）的替换入口。流水线在调用时经该门面解析这些协作者，**既有 monkeypatch 继续生效**。
- 新增配置 `knowledge_sink.auto_git_commit`（默认 `false`）：P04 写入全部校验通过后，把本批写出的知识文档与 Skill 文件提交到它们所在的本地 Git 仓库。只暂存并提交本批路径，不碰无关改动，不 push；Git 侧任何失败只留审计，不回滚、不失败批次。同步更新 `flywheel.yaml.example` 与 `AGENTS.md` 包地图 / P04 副作用列。
- 新增 `tests/test_pipeline_git_commit.py`：门面身份与行数、门面 monkeypatch 生效、默认关闭零提交、开启时只提交本批文件（含干扰项不动）、仓库外留痕且运行仍 `PASS`。

## Capabilities

### New Capabilities
（无新增能力：拆分不改变既有行为。）

### Modified Capabilities
- `workflow-orchestration`：新增「模块化流水线包与兼容门面」与「P04 可选的写入后本地 Git 提交」两条增量要求；既有 P00–P07 阶段语义、审批门、锁与 fail-run 契约保持不变。

## Impact

- 后端新增：`core/pipeline/{__init__,constants,events,helpers,recovery,coordinator,git_commit}.py`。
- 后端修改：`core/orchestrator.py`（1845 行 → 48 行门面）、`config.py`（新增 `auto_git_commit`）。
- 测试：新增 `tests/test_pipeline_git_commit.py`；既有导入路径、`ai_flywheel.core.orchestrator.build_runner` 等替换点零改动。
- 文档：`flywheel.yaml.example` 新增带注释的 `auto_git_commit: false`；`AGENTS.md` 包地图、P04 副作用列、节点注册表位置与 hot-file 列表同步。
- 不改变：P00–P07 阶段副作用边界、审批门与认领事务、独占锁与心跳、失败契约、门户 API 与前端契约、默认关闭的 Git 行为。

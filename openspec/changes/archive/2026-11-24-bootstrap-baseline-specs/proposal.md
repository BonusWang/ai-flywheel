## Why

项目已迭代到 v0.3，但行为契约只存在于代码与提交记录里，没有任何规格基线：后续改动（包括缺陷修复）无法回答"改之前系统承诺了什么、改之后承诺变了什么"。本变更是采用 OpenSpec 作为开发流程后的首轮，为既有系统建立初始框架——项目上下文加首批基线规格，让之后的每个变更都有可比对的契约。

## What Changes

- 在 `openspec/config.yaml` 写入项目上下文与本仓库的 OpenSpec 使用约定（命名、文档语言、证据分级、棕地规则、归档策略）。
- 为两个核心能力建立**现状基线**（只描述已有行为，不改变任何代码）：
  - `knowledge-write-gate`：知识文档受控写入与 Wiki 门禁。
  - `workflow-orchestration`：P00–P07 工作流编排。
- 其余能力（会话初筛、预检激活与规则生命周期、错题观察池、Skill 写入、MCP 与门户、交付、蒸馏与核心索引卡）本轮不建基线，首次触及时按棕地规则补齐。

## Capabilities

### New Capabilities
- `knowledge-write-gate`: 知识文档写入链路——路径白名单、隐私门禁、Schema 感知写前校验、原子写入与回读、写后 lint / D2 后检与回滚、通用计划文档的 T5 归一化、索引同步与时效登记。
- `workflow-orchestration`: 一次 P00–P07 运行的完整生命周期——独占锁、节点状态机、准备 / 准入 / 审查 / 计划、审批门、fail-run 契约、结算、报告与交付。

### Modified Capabilities
无（首轮，尚无既有规格）

## Impact

- 仅新增文档：`openspec/config.yaml`、`openspec/specs/**`（归档后生成）。
- 不改动任何代码、配置模板或运行时行为。
- 基线取材：`backend/ai_flywheel/adapters/knowledge.py`、`adapters/wiki_gate.py`、`adapters/runners.py`、`core/orchestrator.py`、`core/privacy.py` 与 `tests/`。

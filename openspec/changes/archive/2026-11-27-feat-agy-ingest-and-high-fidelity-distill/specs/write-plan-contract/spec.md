# write-plan-contract 变更规格

## ADDED Requirements

### Requirement: P03 契约级草稿容量、阶段规划模型与领域页独立名额
系统 SHALL 提升 P03 写入计划的文档深度与多事件覆盖能力，防止领域增量页挤占主经验卡名额：
- P03 提示词 MUST 移除“220–450 字符浅摘要”硬限制，改为引导模型按挂载 Wiki 契约产出高信息密度、去重复废话的完整结构化正文（实战经验卡 ≤80 行、轻记卡 ≤20 行、领域增量仅写实战已证差量）；当 P02 产出多个独立 `events` 时，P03 提示词 MUST 指导按事件分卡、禁止用事故卡吞并业务交付事实；
- `ApiRunnerConfig` MUST 支持可选 `plan_model` 与 `plan_reasoning_effort`（为空时回退至 `model` / `reasoning_effort`），P03 调用与缓存键 MUST 使用阶段生效配置；
- `WorkflowConfig` MUST 提供 `max_domain_writes_per_run`（默认 `8`），用于约束单批项目中领域增量页（`deposit_routing` 命中的表结构/配置/架构/算法页）的名额上限，避免单次排查涉及多张表时将主经验卡或规则挤出单批计划；最终物理写入总数仍 MUST 严格受 `knowledge_sink.max_write_set` 与高写入审批阈值约束。

#### Scenario: P03 使用独立 plan_model 且不再限制 220-450 字浅摘要
- **WHEN** 配置 `runner.api.plan_model = "strong-plan-model"` 并构建 P03 提示词
- **THEN** P03 请求体必须发送 `model = "strong-plan-model"`，且提示词不得再包含 `220-450 chars` 的浅摘要字数限制

#### Scenario: 多张领域表页不挤占主经验卡名额
- **WHEN** 一批审查同时产出 1 条主经验卡与 4 张项目数据库表结构增量页，`max_writes_per_run = 4`、`max_domain_writes_per_run = 6`、`knowledge_sink.max_write_set = 12`
- **THEN** 主经验卡与领域表结构页均可进入写入计划，且高写入审批阈值仍按最终写入项总数正常触发

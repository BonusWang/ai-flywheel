## MODIFIED Requirements

### Requirement: 计划输入与提示词
P03 提示词 SHALL 包含阶段、值得沉淀的审查发现、可晋升错题、重复流程候选、既有规则摘要与既有 Skill 名，并要求模型只返回 JSON。提示词带有路由候选集时，输入 MUST 包含该候选集：可路由页面类型，以及每个候选项目的目录名、`project_id`、别名与描述。候选集为空（没有可路由页面类型或候选项目）时，提示词 MUST 告知模型不要输出 `knowledge_doc` 项，且 MUST NOT 要求路由声明。执行器收到的写入项超过单批上限时 MUST 只保留上限内的项。

#### Scenario: 提示词带齐计划输入
- **WHEN** 生成 P03 提示词
- **THEN** 提示词包含阶段、值得沉淀的审查发现、可晋升错题、重复流程候选、既有规则摘要与既有 Skill 名

#### Scenario: 路由候选集进入提示词输入
- **WHEN** 挂载 Schema 治理的 Wiki 生成 P03 提示词，且存在可路由页面类型与候选项目
- **THEN** 提示词的输入 JSON 含路由候选集，逐项列出可路由页面类型与候选项目的目录名、`project_id`、别名与描述

#### Scenario: 候选集为空时不要求声明
- **WHEN** 挂载 Schema 治理的 Wiki 没有可路由页面类型或没有候选项目
- **THEN** 提示词告知模型不要输出 `knowledge_doc` 项，且不含路由声明的要求

#### Scenario: 未挂载 Wiki 时不涉及路由
- **WHEN** 知识落点不是 Schema 治理的 Wiki
- **THEN** P03 提示词不含路由候选集，也不要求路由声明

### Requirement: 输出契约列明必填字段
P03 提示词 MUST 在输出契约里逐项列明每个计划项必带的字段：`target_kind`、`action`、`title`、`summary`、`relative_path`、`source_session_keys`，以及各类型的专属字段。提示词 MUST 写明：Skill 项动作为 `update` 时路径必须是现有 Skill 的目录；没有可写入项时返回空的 `planned_writes`。格式修复重试给出的期望结构 MUST 包含同样的必填字段。

提示词带有路由候选集时，输出契约 MUST 要求每项 `knowledge_doc` 附带路由声明 `wiki_route`（页面类型、项目、理由），并写明：页面类型与项目只能取自候选集；笔记只是顺带提到某项目、跨多个项目、审计或维护知识库本身时声明无归属；不得编造候选集之外的页面类型或项目。此时格式修复重试给出的期望结构 MUST 同样包含 `wiki_route`。

#### Scenario: 提示词的逐项骨架包含必填字段
- **WHEN** 生成 P03 提示词
- **THEN** 输出契约的逐项骨架同时包含六个必填字段，并写明空计划的返回方式与 Skill 更新的目标约束

#### Scenario: 知识文档项要求路由声明
- **WHEN** 生成带路由候选集的 P03 提示词
- **THEN** 输出契约的逐项骨架为知识文档项列出 `wiki_route` 的页面类型、项目与理由，并写明取值只能来自候选集以及何时声明无归属

#### Scenario: 格式修复的期望结构与契约一致
- **WHEN** P03 输出触发格式修复重试
- **THEN** 修复提示词给出的期望结构包含六个必填字段；提示词带路由候选集时还包含 `wiki_route`

## ADDED Requirements

### Requirement: 路由声明随计划项透传
`knowledge_doc` 计划项的路由声明（`wiki_route`）SHALL 原样保留在计划项上，供编排器做确定性校验。声明不是对象时 MUST 按缺失处理：计划项保留、声明置空；执行器不得因此丢弃该项，也不得替它补一个页面类型或项目。

#### Scenario: 声明原样保留
- **WHEN** 模型给某知识文档项的 `wiki_route` 带页面类型、项目与理由
- **THEN** 计划项上的 `wiki_route` 与模型输出一致

#### Scenario: 畸形声明按缺失处理
- **WHEN** 模型给某知识文档项的 `wiki_route` 是一个字符串
- **THEN** 该项仍进入写入计划，其 `wiki_route` 为空，随后由路由校验判为缺少声明

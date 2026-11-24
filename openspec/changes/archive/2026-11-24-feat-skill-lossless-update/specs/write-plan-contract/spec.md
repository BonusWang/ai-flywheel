# write-plan-contract —— 写入计划契约

> 本次只改动与 Skill 更新有关的两条要求，其余要求保持现行基线。

## MODIFIED Requirements

### Requirement: 计划输入与提示词
P03 提示词 SHALL 包含阶段、值得沉淀的审查发现、可晋升错题、重复流程候选、既有规则摘要与既有 Skill 名，并要求模型只返回 JSON。提示词 MUST 写明本次最多输出多少条写入项，该上限 MUST 取配置的单批写入上限（不小于 1），不得写死成更小的数；提示词 MUST 提示模型为值得沉淀的经验卡留出位置——规则或 Skill 不得挤掉值得沉淀的会话经验卡。提示词带有路由候选集时，输入 MUST 包含该候选集：可路由页面类型，以及每个候选项目的目录名、`project_id`、别名与描述。候选集为空（没有可路由页面类型或候选项目）时，提示词 MUST 告知模型不要输出 `knowledge_doc` 项，且 MUST NOT 要求路由声明。执行器收到的写入项超过单批上限时 MUST 只保留上限内的项。

本批要更新的现有 Skill MUST 以 `existing_skill_sections` 进入提示词输入：键是 Skill 名，值是它已有的二级标题清单；只列出本批重复流程候选命中的 Skill。提示词 MUST 写明 Skill 项两种动作各自的输出契约：动作为 `create` 时输出带 YAML frontmatter 的完整 `SKILL.md`；动作为 `update` 时只输出追加到现有文件末尾的增量小节，以二级标题开头，不带 frontmatter、不带一级标题、不重写已有小节，且不重复 `existing_skill_sections` 里已有的标题。

#### Scenario: 提示词带齐计划输入
- **WHEN** 生成 P03 提示词
- **THEN** 提示词包含阶段、值得沉淀的审查发现、可晋升错题、重复流程候选、既有规则摘要与既有 Skill 名

#### Scenario: 提示词的条数上限取配置值
- **WHEN** 配置的单批写入上限为 8
- **THEN** 提示词写明最多输出 8 条，并提示为经验卡留出位置

#### Scenario: 路由候选集进入提示词输入
- **WHEN** 挂载 Schema 治理的 Wiki 生成 P03 提示词，且存在可路由页面类型与候选项目
- **THEN** 提示词的输入 JSON 含路由候选集，逐项列出可路由页面类型与候选项目的目录名、`project_id`、别名与描述

#### Scenario: 候选集为空时不要求声明
- **WHEN** 挂载 Schema 治理的 Wiki 没有可路由页面类型或没有候选项目
- **THEN** 提示词告知模型不要输出 `knowledge_doc` 项，且不含路由声明的要求

#### Scenario: 未挂载 Wiki 时不涉及路由
- **WHEN** 知识落点不是 Schema 治理的 Wiki
- **THEN** P03 提示词不含路由候选集，也不要求路由声明

#### Scenario: 现有 Skill 的小节清单进入提示词输入
- **WHEN** 本批的重复流程候选命中一个现有 Skill
- **THEN** 提示词的输入 JSON 含 `existing_skill_sections`，键是该 Skill 名、值是它已有的二级标题清单

#### Scenario: 更新项只输出增量小节
- **WHEN** 生成 P03 提示词
- **THEN** 提示词写明 `create` 输出完整 `SKILL.md`，`update` 只输出追加到现有文件末尾的增量小节且不重复已有标题

### Requirement: Skill 计划项的动作与目标一致
Skill 计划项动作为 `update` 时，目标目录 MUST 是现有 Skill 目录（存在 `SKILL.md`），否则 MUST 丢弃该项并留痕，不得另起新目录。动作为 `create` 而目标目录已存在时，系统 MUST 把动作改标为 `update`，并在诊断里记下改标。

动作为 `update` 的计划项，其正文 MUST 是追加到现有文件末尾的小节：目标 `SKILL.md` 不存在时 MUST 丢弃该项并留痕（原因码 `UPDATE_TARGET_MISSING`）；目标 `SKILL.md` 的 frontmatter 不合规时 MUST 丢弃该项并留痕（原因码 `UPDATE_TARGET_INVALID`）；追加内容与现有文件完全重复时 MUST 丢弃该项并留痕（原因码 `UPDATE_NO_NEW_CONTENT`）。系统 MUST 把归一化后的小节存为该项的正文预览，使审批人看到的正是将要追加的内容。

动作为 `update` 的计划项 MUST 在审批理由里写明：在现有文件末尾追加小节，原有 N 行完整保留，原文件自动备份；MUST NOT 再写整文件替换。

#### Scenario: 更新不存在的 Skill 被丢弃
- **WHEN** 模型的 Skill 项动作为 `update`，而目标目录下没有 `SKILL.md`
- **THEN** 该项被丢弃，原因为更新目标不存在（`UPDATE_TARGET_MISSING`），技能目录没有新增目录

#### Scenario: 新建已存在的 Skill 改标为更新
- **WHEN** 模型的 Skill 项动作为 `create`，而目标目录下已有 `SKILL.md`
- **THEN** 该项的动作变为 `update`，审批理由说明目标已存在且按追加处理，诊断记为改标

#### Scenario: 更新现有 Skill 的审批理由写明追加方式
- **WHEN** 模型的 Skill 项动作为 `update`，目标目录下已有合规的 `SKILL.md`
- **THEN** 该项进入计划，审批理由写明在现有文件末尾追加小节、原有的行数完整保留并自动备份

#### Scenario: 更新目标不合规时丢弃
- **WHEN** 模型的 Skill 项动作为 `update`，而目标 `SKILL.md` 的 frontmatter 不合规
- **THEN** 该项被丢弃，原因为更新目标不合规（`UPDATE_TARGET_INVALID`）

#### Scenario: 追加内容与现有内容完全重复时丢弃
- **WHEN** 模型的 Skill 项动作为 `update`，而它写的每一行内容都已在目标文件里
- **THEN** 该项被丢弃，原因为没有新增内容（`UPDATE_NO_NEW_CONTENT`）

#### Scenario: 预览就是将要追加的小节
- **WHEN** 一个 `update` 的 Skill 计划项通过校验
- **THEN** 该项的正文预览是归一化后将要追加的那一小节，不含 frontmatter 与一级标题

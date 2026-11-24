# write-plan-contract Specification

## Purpose
写入计划契约：P03 执行器的选择、提示词与输出契约、可补全字段的确定性补全、无效计划项的丢弃与留痕、计划来源的唯一性（模型计划不可用时失败而不是换计划器），以及本地启发式计划器的产出边界（只产知识文档与规则）。
## Requirements
### Requirement: 执行器选择
P03 的执行器 SHALL 按配置选择：`api` 模式且已配置地址与模型时用 OpenAI 兼容 API，`cli` 模式且已配置命令时用本地 CLI，其余情况用零成本的本地启发式执行器。安全阀 `CLI_EXECUTION_ENABLED` 关闭时 MUST 不调用模型或 CLI。

#### Scenario: 未配置模型时用本地启发式执行器
- **WHEN** `runner.mode` 为 `api` 但未配置 `base_url` 或 `model`
- **THEN** P03 由本地启发式执行器完成，不发起任何网络请求

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

### Requirement: 输出缓存与格式修复
API 执行器 SHALL 按阶段、地址、模型、推理强度与提示词缓存已解析的输出，命中缓存时 MUST 不重复调用模型。缓存 MUST 只在输出通过语义校验（能解析出 `planned_writes` 列表，且至少有一项通过校验）之后写入；未通过校验的输出 MUST NOT 落缓存，也 MUST NOT 被下一次同批调用命中。磁盘上已存在同一个被读到的坏缓存文件时，失败信息 MUST 给出该文件名，以便删除后重答。输出不是合法 JSON 时 MUST 只做一次仅修 JSON 语法的重试，不重跑审查环节。

计划诊断 SHALL 随计划给出回复轨迹：回复字符数、是否做过格式修复、解析失败的原因码、修复调用本身失败时的异常类型，以及本次模型调用的留痕（尝试次数、耗时、实际服务的模型与 token 用量）。诊断 MUST NOT 包含回复正文。

#### Scenario: 畸形输出只重试一次
- **WHEN** 模型首次返回的 P03 输出不是合法 JSON
- **THEN** 执行器发起一次格式修复重试，修复后的输出进入计划归一化

#### Scenario: 不可用的输出不落缓存
- **WHEN** 模型的计划项全部未通过校验
- **THEN** 执行器不写缓存文件，同一提示词的第二次调用重新询问模型，失败原因不变

#### Scenario: 已存在的坏缓存文件被指名
- **WHEN** 磁盘上已有同一批次的坏缓存文件且被读到
- **THEN** 失败信息给出该缓存文件名，并说明删除后可让模型重答

#### Scenario: 计划诊断带回复轨迹与调用留痕
- **WHEN** 模型返回的输出需要一次格式修复
- **THEN** 计划诊断记下回复字符数、做过格式修复、解析错误与本次调用的留痕，且不含回复正文

### Requirement: 规则载荷归一化
`flywheel_rule` 项的动作 MUST 固定为 `register_rule`，其规则载荷 MUST 经确定性归一化（标题、领域、项目、触发词、检查项、禁忌路线、来源指针）后才能进入写入环节；模型使用别名键时 MUST 把内容映射到对应字段。

#### Scenario: 别名键被映射
- **WHEN** 规则项的载荷使用 `scope`、`requirement` 等别名键而没有 `checklist`
- **THEN** 归一化后的规则带标题与触发词，别名键的内容并入检查项

### Requirement: 知识文档默认路径
`knowledge_doc` 项未给路径时，系统 SHALL 按 `{默认前缀}{当天}-{标题转写}.md` 生成路径。标题是原始客户端加编号的形态时 MUST 改取摘要或正文的首个分句。

#### Scenario: 未给路径的知识文档
- **WHEN** 计划项是 `knowledge_doc`，没有 `relative_path`，标题为「接口超时排查」
- **THEN** 该项路径为默认前缀下以当天日期与标题转写命名的 Markdown 文件

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

### Requirement: 可补全字段的确定性补全
计划项缺少 `title` 或 `summary` 时，系统 SHALL 按固定顺序取值补全，不得因此丢弃该项：

- `title`：规则项依次取载荷标题、首条检查项、正文首句；Skill 项依次取正文一级标题、路由目标名、正文首句；其余项依次取 frontmatter 的 `title`、正文一级标题、正文首句；
- `summary`：规则项依次取首条检查项、标题；Skill 项依次取 frontmatter 的 `description`、路由理由、审批理由、正文首句、标题；其余项依次取正文首句、标题；
- 正文首句 MUST 取去掉 frontmatter、标题行、列表与强调标记后的第一句完整话。

补全了哪些字段 MUST 计入计划诊断。补全后仍为空的字段，按"无效计划项的丢弃与留痕"处理。

#### Scenario: 缺摘要的知识文档被保留
- **WHEN** 模型的知识文档项没有 `summary`，但有标题和正文
- **THEN** 该项进入写入计划，`summary` 为正文首句；正文没有完整句子时取标题

#### Scenario: 缺标题与摘要的规则项被保留
- **WHEN** 模型的规则项没有 `title` 与 `summary`，但载荷带标题和检查项
- **THEN** 该项的标题取载荷标题，摘要取首条检查项，诊断记为补全了 `title` 与 `summary`

#### Scenario: 缺标题与摘要的 Skill 项被保留
- **WHEN** 模型的 Skill 项没有 `title` 与 `summary`，没有一级标题，但 frontmatter 带 `description`，路由带目标名与理由
- **THEN** 该项的标题取路由目标名，摘要取 `description`

#### Scenario: 正文首句不含标记
- **WHEN** 知识文档的正文首行是二级标题，其后是带强调标记的列表项
- **THEN** 补全出的首句是列表项去掉列表符号与强调标记后的那句话

### Requirement: 无效计划项的丢弃与留痕
补全后仍不合规的计划项 MUST 被丢弃，不得进入写入计划。不合规包括：不是对象；补全后仍缺必填字段或类型不符；`target_kind` 不受支持；知识文档或 Skill 的正文为空；正文或规则标题与检查项里出现未脱敏的凭据；归一化过程出错。执行器收到的写入项多于单批上限时，上限之外的项 MUST 计入"超出单批上限"。

单批上限 MUST 按保留下来的写入项数计，不得按读到的项序号计：不合规的项 MUST NOT 占用后面合规项的名额。

每个被丢弃或未列入的计划项 MUST 留下序号、类型、动作与原因，并写入日志和计划诊断；日志与诊断 MUST NOT 包含计划项的正文。

#### Scenario: 部分丢弃进入诊断与日志
- **WHEN** 模型返回 5 项，其中 1 项的 `target_kind` 不受支持
- **THEN** 执行器返回 4 项计划，诊断列出被丢弃项的序号、类型与原因，日志有一条告警

#### Scenario: 不合规项不吃掉后面的名额
- **WHEN** 模型返回的前两项都不合规，后面三项合规，且单批上限为 3
- **THEN** 执行器返回 3 项计划，两个不合规项被丢弃并留痕

#### Scenario: 超出上限的项计入诊断
- **WHEN** 模型返回 7 项而单批上限为 5
- **THEN** 执行器返回 5 项计划，诊断把其余 2 项记为超出单批上限

#### Scenario: 正文为空的计划项被丢弃
- **WHEN** 知识文档项补全后正文为空
- **THEN** 该项被丢弃，原因为正文为空

#### Scenario: 带凭据的计划项被丢弃
- **WHEN** 知识文档项的正文里含未脱敏的凭据
- **THEN** 该项被丢弃，原因为检测到密钥泄漏，正文里的 `<private>` 片段已被脱敏而不触发丢弃

### Requirement: 计划项的隐私前置门
计划项进入写入环节前 MUST 过隐私前置门：正文里的 `<private>...</private>` 片段 MUST 先脱敏再进入 SQLite 与审批预览；规则项的标题与检查项同样 MUST 过门。检测到凭据时整项 MUST 丢弃并留痕（`SECRET_LEAK_BLOCKED`），不得把带凭据的正文写进数据库或审批预览。写入环节的隐私与密钥门禁 MUST 保留，作为第二道防线。

#### Scenario: 私有片段在审批预览前被脱敏
- **WHEN** 知识文档项的正文含 `<private>` 片段
- **THEN** 计划项正文里该片段被替换为脱敏标记，原文不进入数据库与审批预览

#### Scenario: 规则标题里的凭据让整项被丢弃
- **WHEN** 规则项的标题或检查项含未脱敏的凭据
- **THEN** 该项被丢弃并留痕，不进入写入计划

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

### Requirement: 计划来源唯一
`api` 与 `cli` 模式下，P03 执行器 MUST 只返回模型或 CLI 输出经补全与校验后的计划项。没有可解析的输出，或返回的计划项全部被丢弃时，执行器 MUST 报告计划不可用并携带逐项原因，不得返回本地启发式计划。模型返回空列表 MUST 视为无可写入项。

执行器 SHALL 随计划给出诊断：计划来源、返回项数、保留项数、补全字段统计、丢弃与未列入明细、缺来源会话的项数。`dry_run` 模式下计划来源为本地启发式计划器。

#### Scenario: 全部无效时报告计划不可用
- **WHEN** `api` 模式下模型返回 5 项，补全后仍全部不合规
- **THEN** 执行器报告计划不可用并携带 5 项的原因，不返回任何计划项，也不调用本地启发式计划器，也不写缓存文件

#### Scenario: 模型明确返回空计划
- **WHEN** `api` 模式下模型返回空的 `planned_writes`
- **THEN** 执行器返回空计划，诊断的返回项数为 0，不报告计划不可用

#### Scenario: 输出无法解析
- **WHEN** 格式修复重试后仍解析不出 `planned_writes` 列表
- **THEN** 执行器报告计划不可用，不调用本地启发式计划器

#### Scenario: 诊断如实反映补全与来源会话缺失
- **WHEN** 模型返回 5 项，全部缺 `summary` 与 `source_session_keys`，补全后全部合规
- **THEN** 计划为 5 项，诊断记为补全 `summary` 5 项、缺来源会话 5 项，来源为模型

### Requirement: 启发式计划器不产出 Skill 写入
本地启发式计划器 SHALL 只产出知识文档与规则。重复流程候选 MUST 转为"未生成 Skill 计划"的候选记录，写入计划诊断，不得以模板正文生成 Skill 写入。候选记录 SHALL 含来源会话、标题、意向动作与目标 Skill；意向动作为 `update` 时，目标 MUST 是候选给出的现有 Skill 名。

#### Scenario: 重复流程候选只留痕
- **WHEN** 启发式计划器收到 2 个重复流程候选，其中 1 个意向动作为 `update` 并带现有 Skill 名
- **THEN** 计划里没有 Skill 项，诊断含 2 条候选记录，`update` 候选的目标是该现有 Skill 名

#### Scenario: 没有候选时不产生记录
- **WHEN** 审查结果里没有重复流程候选
- **THEN** 诊断里没有候选记录

### Requirement: 路由声明随计划项透传
`knowledge_doc` 计划项的路由声明（`wiki_route`）SHALL 原样保留在计划项上，供编排器做确定性校验。声明不是对象时 MUST 按缺失处理：计划项保留、声明置空；执行器不得因此丢弃该项，也不得替它补一个页面类型或项目。

#### Scenario: 声明原样保留
- **WHEN** 模型给某知识文档项的 `wiki_route` 带页面类型、项目与理由
- **THEN** 计划项上的 `wiki_route` 与模型输出一致

#### Scenario: 畸形声明按缺失处理
- **WHEN** 模型给某知识文档项的 `wiki_route` 是一个字符串
- **THEN** 该项仍进入写入计划，其 `wiki_route` 为空，随后由路由校验判为缺少声明


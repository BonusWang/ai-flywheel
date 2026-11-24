# knowledge-write-gate Specification

## Purpose
知识文档的受控写入链路：路径白名单、隐私与密钥门禁、针对挂载 Schema 的写前校验、原子写入与回读、写后 lint 与 D2 后检（失败回滚）、把通用计划文档归一化为 Wiki 经验卡，以及写入后的索引同步与时效登记。
## Requirements
### Requirement: 写入路径白名单
系统 SHALL 只允许写入落在 `knowledge_sink.allowed_write_prefixes` 之内、且解析后仍位于知识库根目录内的相对路径；任一条件不满足 MUST 抛出 `KnowledgeWriteError`，且不创建、不修改任何文件。

#### Scenario: 路径穿越被拒绝
- **WHEN** 相对路径解析后逃出知识库根目录
- **THEN** 抛出 `KnowledgeWriteError`（Path traversal blocked），目标文件不被创建或修改

#### Scenario: 前缀外路径被拒绝
- **WHEN** 已配置 `allowed_write_prefixes`，且目标路径不以其中任一前缀开头
- **THEN** 抛出 `KnowledgeWriteError`，目标文件不被创建或修改

### Requirement: 写时隐私与密钥门禁
每次知识文档写入 MUST 先经过隐私门：`<private>...</private>` 区块被替换为 `[REDACTED_PRIVATE]`；内容命中已知密钥形态（OpenAI / Anthropic Key、GitHub Token、AWS Access Key、Slack Token、私钥块）时 MUST 抛出 `SecretLeakError` 并放弃写入。

#### Scenario: 私有区块被脱敏
- **WHEN** 文档内容包含 `<private>` 区块且不含密钥形态
- **THEN** 落盘内容中该区块被替换为 `[REDACTED_PRIVATE]`，写入继续

#### Scenario: 密钥泄露被拦截
- **WHEN** 文档内容命中任一密钥形态
- **THEN** 抛出 `SecretLeakError`，不进入 Schema 校验，也不写文件

### Requirement: Schema 感知的写前门禁
当知识库根目录存在 `.wiki-schema.md` 且 `wiki_gate_enabled` 为真时，系统 MUST 在写盘前读取该文件的 `core_contract` / `partition_contract` / `content_contract`（按文件修改时间缓存），并按页面类型逐项校验：

- 核心文件（`core_files`，按相对路径或文件名匹配）永不可直接写入；
- `raw` / `archive-*` / 过程目录路径段不可写入；
- frontmatter 必须从文件首字节开始，不得带 BOM，必须是合法 YAML 映射；
- `type` 必填、不得是遗留别名、必须是已声明的页面类型；
- 通用必填字段、页面类型必填字段、条件必填字段齐全；状态、加载策略、取值枚举合规；
- 日期必须是真实 ISO 日期且不晚于当天；
- 经验卡（`project-practice-note` / `project-practice-lite`）字段不得超出白名单，`description` 不超过 120 字，`tags` 为不超过 8 项的列表，`load_policy` 为 `on_demand`，`default_load` 为假，不得使用遗留的原文 / 提炼双层目录；
- 目标路径必须位于该页面类型声明的分区内并匹配路径 / 文件名模式；
- 凭据页只能位于 `{project}/项目配置/` 下，且加载策略为 `explicit_or_task_required`、`default_load` 为假、带 `access_scope`；
- 正文骨架：恰好一个 H1；T5 重卡 H2 必须依次为 好流程 / 错误示范 / 关键事项 / 遗留与待办 / 证据指针，H1 后首行为 `> 背景与结论：`，证据指针之后有 `> 分流判定：`，正文不超过 80 行；T5b 轻记卡 H2 必须依次为 结论 / 适用范围 / 来源，正文不超过 20 行，且不带背景与分流判定引注。

任一项不满足 MUST 抛出 `WikiGateError`（列出全部问题码），并在门禁证据目录落一份 `pre_write` 证据 JSON，且不写文件。根目录没有 `.wiki-schema.md` 或门禁被关闭时，门禁不适用（`skipped_reason` 为 `not_a_schema_governed_wiki_root`），写入不做 Schema 校验。

#### Scenario: 核心文件拒写
- **WHEN** 目标是 Schema 声明的核心文件，即使写入前缀允许该路径
- **THEN** 抛出 `WikiGateError`（CORE_FILE_BLOCKED），核心文件字节不变

#### Scenario: 缺少页面类型
- **WHEN** 文档 frontmatter 没有 `type` 字段
- **THEN** 抛出 `WikiGateError`（FIELD_REQUIRED，字段 `type`），不写文件，并留下 `pre_write` 证据

#### Scenario: 经验卡放置不合规
- **WHEN** 页面类型声明的路径模式与目标相对路径不匹配
- **THEN** 抛出 `WikiGateError`（PATH_PATTERN_MISMATCH 或 FILE_PATTERN_MISMATCH），不写文件

#### Scenario: 无 Schema 的知识库
- **WHEN** 知识库根目录不存在 `.wiki-schema.md`
- **THEN** 门禁判定为不适用，写入不做 Schema 校验

### Requirement: 原子写入与回读校验
通过写前校验后，系统 SHALL 先写临时文件再原子替换目标文件，并回读比对；回读内容与预期不一致 MUST 抛出 `KnowledgeWriteError`。动作为 `update` 而写入前后字节完全相同时 MUST 抛出 `KnowledgeWriteError`。

#### Scenario: 更新未产生字节变化
- **WHEN** 以 `update` 动作写入的内容与目标文件现有字节完全相同
- **THEN** 抛出 `KnowledgeWriteError`（Update action produced no byte change）

### Requirement: 写后 lint、D2 后检与失败回滚
门禁适用时，写盘后系统 SHALL 对 `topic` 页面运行 Wiki lint，并在 lint 通过后运行 D2 后检脚本（传入知识库根、写后变更路径、当天日期与 JSON 输出路径）。lint 或 D2 未通过时，系统 MUST 回滚本次写入（新文件则删除，已有文件则恢复写前字节），重跑一次跳过校验器的 D2 以重同步索引，落 `post_write` 证据（含回滚结果），再抛出 `WikiGateError`。脚本未配置且 `wiki_gate_require_scripts` 为真时 MUST 判为错误并回滚；为假时记为跳过。

#### Scenario: D2 判定阻断
- **WHEN** D2 脚本以退出码 3 结束
- **THEN** 本次写入被回滚，抛出 `WikiGateError`（D2_BLOCKED），证据记录回滚状态与索引重同步状态

#### Scenario: D2 通过
- **WHEN** lint 与 D2 均返回 0
- **THEN** 写入保留，返回结果 `verified` 为真并附 `preimage_sha256` / `postimage_sha256` 与门禁详情，落 `post_write` 证据

#### Scenario: 后检脚本缺失
- **WHEN** `wiki_gate_require_scripts` 为真而 D2 脚本未配置
- **THEN** 判为 D2_SCRIPT_MISSING 错误，本次写入被回滚

### Requirement: 通用计划文档归一化为 T5 经验卡
当门禁适用时，系统 SHALL 提供归一化入口，把 P03 产出的通用知识文档改写为挂载 Wiki 可接受的 `project-practice-note`（T5）经验卡：

- 门禁不适用或 Schema 读取失败时，原样返回；
- 草稿在其目标路径上已通过写前校验时 MUST 以路径为准：页面类型属于可路由页面类型、且路由结论的项目与路径所示项目一致时原样返回，否则 MUST 返回"无法归属"；
- 其余情况 MUST 先按"知识文档路由声明校验"得出路由结论再落点：页面类型与项目取自结论；结论为不写入时 MUST 返回"无法归属"，不得选择任何项目；
- 目标路径为 `{项目分区根}/{项目目录}/实战笔记/{当天}-{标题转写}.md`；frontmatter 含标题、描述、类型、`project_id`、`record_id`（当天加标题转写，ASCII 转写过短时追加标题摘要保证唯一）、`event_date`、`load_policy: on_demand`、`default_load: false`、创建 / 更新日期、`status: active`、时效三字段（档位默认 `evolving`，有效期按档位天数推算）、标签与来源；
- 正文为 T5 骨架：H1、`> 背景与结论：{描述}`、好流程、错误示范、关键事项、遗留与待办、证据指针、`> 分流判定：` 尾注；各段落内容 MUST 按"经验卡正文保真映射"生成。

工作流编排 SHALL 在 P03 对每项 `knowledge_doc` 计划先做路由声明校验，再带着路由结论调用该入口，同批归一化到同一路径时追加序号后缀；并在 P04 写入前再次调用。

#### Scenario: 已合规的 T5 卡原样保留
- **WHEN** 计划文档在其目标路径上已通过写前校验，页面类型为 `project-practice-note`，路由结论的项目与路径所示项目一致
- **THEN** 路径与内容保持不变

#### Scenario: 已合规卡与声明项目不一致
- **WHEN** 计划文档在其目标路径上已通过写前校验，但模型声明的项目与路径所示项目不同
- **THEN** 归一化入口返回"无法归属"，不产生目标路径

#### Scenario: 通用草稿按路由结论改写
- **WHEN** 计划文档的 frontmatter 缺少 `type`，路由结论为页面类型 `project-practice-note`、项目 alpha
- **THEN** 文档被改写为 alpha 的 T5 经验卡，并通过写前校验

#### Scenario: 路由结论为不写入
- **WHEN** 路由结论为不写入
- **THEN** 归一化入口返回"无法归属"，不产生目标路径

#### Scenario: 演练计划按工作区证据归属
- **WHEN** 计划不带路由声明且来自零配置演练执行器，来源会话工作区路径含某候选项目的 `project_id`
- **THEN** 文档被改写为该项目的 T5 经验卡，并通过写前校验

#### Scenario: 演练计划无证据时不猜项目
- **WHEN** 计划不带路由声明且来自零配置演练执行器，工作区与草稿全文都没有任何候选项目的标识，且未配置兜底项目
- **THEN** 归一化入口返回"无法归属"，不产生目标路径

#### Scenario: 演练计划配置兜底项目
- **WHEN** 演练执行器的计划没有任何证据，但配置了 `practice_fallback_project` 且对应目录存在
- **THEN** 文档落到该兜底项目

#### Scenario: 同批路径重名
- **WHEN** 同一批两项计划归一化后得到相同路径
- **THEN** 后者文件名追加 `-2` 等序号后缀

### Requirement: 写入后的索引同步与时效登记
每次写入结束后（含回滚后）系统 SHALL 重建知识全文索引：挂载 Schema 时只索引 `formal_active` 范围声明的分区，并排除归档 / 退役 / 占位等状态、重定向页、`raw` / `archive-*` 路径与私密凭据页。写入成功时 MUST 按文档 frontmatter 登记时效：`freshness_tier` 默认 `evolving`（非法取值按 `evolving`），`valid_until` 缺省时按档位对应天数推算（30 / 90 / 180 天）。

#### Scenario: 写入成功后登记时效
- **WHEN** 文档通过全部门禁并成功落盘
- **THEN** 该路径的时效记录被写入（档位、最近核验日、有效期），状态为 fresh

### Requirement: 经验卡正文保真映射
归一化生成的经验卡正文 SHALL 忠实于草稿，不得合成草稿中没有的事实性陈述：

- 草稿按二级标题分桶：事实类（事实回顾及未分节内容）、异常类（异常与根因线索）、前提类（适用前提与失效条件）、分歧类（分歧与反例）、复用类（复用建议）；
- 「好流程」取事实类要点在前、复用类要点在后；「关键事项」取余下事实类要点与前提类要点；「错误示范」只取异常类要点；「遗留与待办」取分歧类要点与复测续期说明；
- 草稿事实类要点 MUST 全部落入「好流程」或「关键事项」，不得因数量上限被丢弃；
- 某一类没有内容时 MUST 如实写"未记录"，不得填入模板化的错误或结论；
- `description` 与「背景与结论」MUST 取有信息量的陈述：摘要不是通用占位（"沉淀来自 <会话> 的协作经验卡"）时取摘要，否则依次取异常类、事实类、复用类的首条，仍无则取标题。

#### Scenario: 真实异常进入错误示范
- **WHEN** 草稿含「异常与根因线索」小节且有一条异常描述
- **THEN**「错误示范」为该条异常描述，且不含任何草稿中没有的错误陈述

#### Scenario: 未记录错误示范
- **WHEN** 草稿没有任何异常类要点
- **THEN**「错误示范」如实标注本次未记录错误示范

#### Scenario: 要点数量超出分段上限
- **WHEN** 草稿事实类要点有 9 条
- **THEN** 9 条全部出现在「好流程」或「关键事项」中

#### Scenario: 描述不写会话编号
- **WHEN** 计划摘要为"沉淀来自 <会话键> 的协作经验卡"
- **THEN** `description` 与「背景与结论」取草稿里的陈述，不含该会话键

### Requirement: 路由候选集发现
当门禁适用时，系统 SHALL 每次运行从挂载 Wiki 动态发现知识文档的路由候选集，不硬编码任何项目名：

- 可路由页面类型：`content_contract.page_types` 中，所属分区在 `partition_contract.partitions` 里可解析到根目录，且落点适配器登记了该类型正文映射器的页面类型；
- 候选项目：项目分区根目录下含 `实战笔记/` 的直接子目录（不含隐藏目录与归档目录）；每个候选带目录名、`project_id`（取自 `实战笔记/笔记索引.md` 的 frontmatter，缺失时为目录名）、别名与描述（同取自该索引页，可缺省）；
- 可路由页面类型或候选项目为空时候选集 MUST 如实为空，不得回退到内置类型或项目；
- 候选集 MUST NOT 依赖知识落点的允许写入前缀：那是配置而非合同，前缀与分区不符时写入仍 MUST 在 P04 显式失败；
- 门禁适用但 Schema 读不出来时 MUST 抛出 `WikiGateError`（运行因此在该节点失败），MUST NOT 当作候选集为空而计划零篇知识文档。

#### Scenario: 合同没有可映射的页面类型
- **WHEN** 挂载 Wiki 的合同没有声明落点适配器能映射的页面类型
- **THEN** 可路由页面类型为空

#### Scenario: 合同读不出来时报告错误
- **WHEN** 门禁适用，但 `.wiki-schema.md` 缺少机器块 `content_contract`
- **THEN** 发现候选集抛出 `WikiGateError` 并指明缺失的机器块，不返回空候选集

#### Scenario: 页面类型的分区无法解析
- **WHEN** 某可映射页面类型声明的分区在分区合同里不存在
- **THEN** 该页面类型不进入候选集

#### Scenario: 允许写入前缀不影响候选集
- **WHEN** 知识落点的允许写入前缀不包含某可映射页面类型所属的分区
- **THEN** 该页面类型仍在候选集内，写入时的前缀不符由写入门禁拒绝

#### Scenario: 项目目录即候选
- **WHEN** 项目分区下某目录含 `实战笔记/`，且其笔记索引声明了 `project_id` 与别名
- **THEN** 该目录成为候选项目，标识包括目录名、`project_id` 与别名

#### Scenario: 不含实战笔记的目录不是候选
- **WHEN** 项目分区下某目录没有 `实战笔记/`
- **THEN** 它不进入候选项目

#### Scenario: 候选随挂载 Wiki 变化
- **WHEN** 项目分区下新增一个含 `实战笔记/` 的目录并移除另一个
- **THEN** 候选项目随之变化，无需改动代码或配置

### Requirement: 知识文档路由声明校验
当门禁适用时，系统 SHALL 在归一化之前对每项 `knowledge_doc` 计划做确定性的路由校验，得出唯一的路由结论（写入或不写入），并给出可留痕的依据：路由来源、页面类型、项目、原因码、模型声明与证据命中。

路由声明是计划项上的 `wiki_route`，含页面类型、项目与理由。校验规则按序为：

- 草稿在其目标路径上已通过写前校验时以路径为准：页面类型不属于可路由页面类型 MUST 判为不写入（`page_type_not_routable`）；不带声明时只有允许证据判定的计划才按路径写入，否则 MUST 判为不写入（`declaration_missing`）；带声明时声明项目 MUST 等于路径所示项目（否则 `path_mismatch`），且同样 MUST 通过下文的冲突校验；
- 可路由页面类型或候选项目为空 MUST 判为不写入（`no_routable_target`）；
- 计划带路由声明时：
  - 声明项目为空 MUST 判为不写入（`declared_none`），并保留模型给出的理由；
  - 页面类型不在可路由页面类型内，或项目不能唯一匹配一个候选（按目录名、`project_id` 或别名精确匹配，忽略大小写）MUST 判为不写入（`declaration_invalid`），不得纠正到最接近的候选；
  - 声明项目与工作区证据冲突 MUST 判为不写入（`workspace_mismatch`）；与内容证据冲突 MUST 判为不写入（`content_mismatch`）；
  - 其余判为写入，页面类型与项目取自声明，路由来源为 `declared`；
- 计划不带路由声明时：零配置演练执行器的计划沿用证据判定（工作区证据优先于内容证据，取项目标识匹配最长的候选），有命中判为写入且路由来源为 `evidence`，无命中时按兜底项目配置处理，仍未命中判为不写入（`no_evidence`）；其他执行器的计划 MUST 判为不写入（`declaration_missing`）。

证据口径与冲突的定义：

- 工作区证据：来源会话工作区路径的某一段等于候选项目的目录名或 `project_id`，或该段包含项目标识（`project_id`、目录名、目录名开头的连续 ASCII 词；ASCII 标识必须落在字母数字边界上）；
- 内容证据：项目标识出现在草稿全文（项目提示、原路径、标题、摘要、正文）中；
- 长度小于 3 的片段与通用词（如 core、web、service）MUST NOT 作为证据；
- 工作区命中非空且不含声明项目即为冲突；工作区命中为空而内容命中非空且不含声明项目即为冲突；证据为空不算冲突，命中包含声明项目不算冲突；
- 声明「无归属」不参与冲突判定。

#### Scenario: 声明与证据一致
- **WHEN** 模型声明项目 alpha，来源会话工作区命中 alpha
- **THEN** 路由结论为写入，页面类型与项目取自声明，路由来源为 `declared`

#### Scenario: 已合规卡同样接受冲突校验
- **WHEN** 模型声明项目 alpha，草稿是 alpha 路径下已通过写前校验的 T5 卡，而来源会话工作区命中项目 beta
- **THEN** 路由结论为不写入，原因码为 `workspace_mismatch`

#### Scenario: 模型执行器的合规卡缺声明
- **WHEN** `api` 执行器的知识文档计划没有路由声明，但草稿在其目标路径上已通过写前校验
- **THEN** 路由结论为不写入，原因码为 `declaration_missing`；摆放位置不能代替声明

#### Scenario: 证据为空不构成冲突
- **WHEN** 模型声明项目 alpha，而工作区与草稿全文都没有任何候选项目的标识
- **THEN** 路由结论为写入，项目为 alpha

#### Scenario: 多个命中包含声明项目
- **WHEN** 模型声明项目 alpha，而草稿全文同时命中 alpha 与 beta
- **THEN** 不构成冲突，路由结论为写入

#### Scenario: 显式无归属
- **WHEN** 模型声明的项目为空并给出理由
- **THEN** 路由结论为不写入，原因码为 `declared_none`，理由被保留

#### Scenario: 项目不在候选集
- **WHEN** 模型声明的项目不能匹配任何候选
- **THEN** 路由结论为不写入，原因码为 `declaration_invalid`，不得改判到其他候选

#### Scenario: 页面类型不在候选集
- **WHEN** 模型声明的页面类型不是可路由页面类型
- **THEN** 路由结论为不写入，原因码为 `declaration_invalid`

#### Scenario: 与工作区证据冲突
- **WHEN** 模型声明项目 beta，而来源会话工作区命中项目 alpha
- **THEN** 路由结论为不写入，原因码为 `workspace_mismatch`

#### Scenario: 与内容证据冲突
- **WHEN** 模型声明项目 beta，工作区没有命中，而草稿全文只命中项目 alpha
- **THEN** 路由结论为不写入，原因码为 `content_mismatch`

#### Scenario: 无归属声明不参与冲突判定
- **WHEN** 模型声明无归属，而来源会话工作区命中项目 alpha
- **THEN** 路由结论为不写入，原因码为 `declared_none`

#### Scenario: 模型执行器的计划缺声明
- **WHEN** `api` 执行器的某项知识文档计划没有路由声明
- **THEN** 路由结论为不写入，原因码为 `declaration_missing`

#### Scenario: 演练执行器的计划走证据判定
- **WHEN** 零配置演练执行器的计划没有路由声明，来源会话工作区命中项目 alpha
- **THEN** 路由结论为写入，项目为 alpha，路由来源为 `evidence`

#### Scenario: 候选集为空
- **WHEN** 挂载 Wiki 没有可路由页面类型或没有候选项目
- **THEN** 不论声明如何，路由结论均为不写入，原因码为 `no_routable_target`


## MODIFIED Requirements

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

## ADDED Requirements

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

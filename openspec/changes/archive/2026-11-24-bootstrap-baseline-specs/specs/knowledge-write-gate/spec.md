# knowledge-write-gate —— 知识文档受控写入与 Wiki 门禁

> 现状基线（2026-11-24，对应 v0.3.0 代码）。只描述系统已经具备的行为。

## ADDED Requirements

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

- 门禁不适用、Schema 读取失败，或草稿在其目标路径上已通过写前校验时，原样返回；
- 否则定位到项目分区根目录下含 `实战笔记/` 的项目目录：第一个其目录名、`project_id` 或目录名分段（长度不小于 2）出现在匹配文本（项目提示 + 原路径 + 标题 + 摘要 + 正文前 600 字符）中的候选；无命中时优先选目录名含 沉淀 / flywheel / general / ai 的候选，再退到第一个候选，都没有则用 `general`；
- 目标路径为 `{项目分区根}/{项目目录}/实战笔记/{当天}-{标题转写}.md`；frontmatter 含标题、描述、类型、`project_id`、`record_id`、`event_date`、`load_policy: on_demand`、`default_load: false`、创建 / 更新日期、`status: active`、时效三字段（`evolving`，有效期 90 天）、标签与来源；
- 正文为 T5 骨架：H1、`> 背景与结论：{描述}`、好流程（草稿前 3 条要点）、错误示范（固定话术）、关键事项（草稿第 4–6 条要点加两条固定说明）、遗留与待办（固定话术）、证据指针、`> 分流判定：` 尾注。

工作流编排 SHALL 在 P03 对每项 `knowledge_doc` 计划调用该入口（带入来源会话的项目与工作区提示），同批归一化到同一路径时追加序号后缀；并在 P04 写入前再次调用。

#### Scenario: 已合规的草稿原样保留
- **WHEN** 计划文档在其目标路径上已通过写前校验
- **THEN** 路径与内容保持不变

#### Scenario: 通用草稿被改写
- **WHEN** 计划文档的 frontmatter 缺少 `type`，且路径直接位于项目分区根目录下
- **THEN** 文档被改写为 T5 经验卡，落到某个项目的 `实战笔记/` 下，并通过写前校验

#### Scenario: 无匹配证据时使用偏好项目
- **WHEN** 没有任何候选项目的名称片段出现在匹配文本中
- **THEN** 选择目录名含 沉淀 / flywheel / general / ai 的第一个候选，否则取第一个候选

#### Scenario: 同批路径重名
- **WHEN** 同一批两项计划归一化后得到相同路径
- **THEN** 后者文件名追加 `-2` 等序号后缀

### Requirement: 写入后的索引同步与时效登记
每次写入结束后（含回滚后）系统 SHALL 重建知识全文索引：挂载 Schema 时只索引 `formal_active` 范围声明的分区，并排除归档 / 退役 / 占位等状态、重定向页、`raw` / `archive-*` 路径与私密凭据页。写入成功时 MUST 按文档 frontmatter 登记时效：`freshness_tier` 默认 `evolving`（非法取值按 `evolving`），`valid_until` 缺省时按档位对应天数推算（30 / 90 / 180 天）。

#### Scenario: 写入成功后登记时效
- **WHEN** 文档通过全部门禁并成功落盘
- **THEN** 该路径的时效记录被写入（档位、最近核验日、有效期），状态为 fresh

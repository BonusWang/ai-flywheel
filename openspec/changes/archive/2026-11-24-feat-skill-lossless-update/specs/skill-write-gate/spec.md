# skill-write-gate —— Skill 受控写入

> 本能力此前没有基线规格。以下 ADDED Requirements 描述本变更落地后的完整行为：写入器的现状能力
> （目录安全、frontmatter 校验、写前备份、原子替换、SHA-256 回读）保持不变；`update` 的语义由
> 「整文件替换」改为「在文件末尾追加一个小节」。

## ADDED Requirements

### Requirement: 写入目标与目录安全
系统 SHALL 只写入技能根目录之内的 `<skill-name>/SKILL.md`；目录名 MUST 为小写字母、数字与连字符组成，长度 2–63 字符。解析后越出技能根目录 MUST 抛出 `SkillWriteError`，且不创建、不修改任何文件。

#### Scenario: 目录穿越被拒绝
- **WHEN** Skill 名解析后位于技能根目录之外
- **THEN** 抛出 `SkillWriteError`（Path traversal blocked），目标文件不被创建或修改

#### Scenario: 目录名不合法时拒绝写入
- **WHEN** Skill 名不是小写字母、数字与连字符组成的合法名
- **THEN** 抛出 `SkillWriteError`，不创建任何目录或文件

### Requirement: 新建 Skill 的写入与校验
`action="create"` 时，系统 SHALL 写入一份完整的 `SKILL.md`：内容 MUST 以 YAML frontmatter 开头且带 `name` 与 `description`，正文 MUST 不短于 20 个字符；行尾 MUST 统一为 LF；写入 MUST 先写临时文件再原子替换，并 MUST 回读比对 SHA-256；命中凭据形态时 MUST 拒绝写入。

#### Scenario: 新建成功后回读校验通过
- **WHEN** 写入一份合规的 `SKILL.md`
- **THEN** 回执的 `sha256` 与落盘内容一致，`verified` 为真

#### Scenario: frontmatter 不合规时拒绝写入
- **WHEN** 内容缺少 `name` 或 `description`，或正文过短
- **THEN** 抛出 `SkillWriteError`，目标文件不被创建

### Requirement: 更新 Skill 只追加不覆盖
`action="update"` 时，目标 `SKILL.md` MUST 已经存在：不存在时 MUST 抛出 `SkillWriteError`（`Cannot update missing skill '<name>'`），MUST NOT 新建目录或新建文件。现有文件的全部字节 MUST 原样保留为新文件的前缀；新内容 MUST 以原文件的一行空行分隔后追加在末尾，并 MUST 沿用原文件的行尾。

写入前 MUST 校验现有文件的 frontmatter 仍然合规；写前 MUST 备份原文件；写入 MUST 走临时文件与原子替换，并 MUST 回读比对 SHA-256。回执 MUST 给出原有行数、新增行数与写入后总行数，并标明本次为追加。

#### Scenario: 更新不存在的 Skill 不会变成新建
- **WHEN** 以 `update` 更新一个没有 `SKILL.md` 的目标
- **THEN** 抛出 `SkillWriteError`，技能目录下不出现新目录

#### Scenario: 原有内容逐字节保留
- **WHEN** 更新一个已有的 `SKILL.md`
- **THEN** 新文件以原文件的全部字节开头，原有内容一字不改，新增小节追加在末尾

#### Scenario: 追加沿用原文件行尾
- **WHEN** 现有文件使用 CRLF 行尾
- **THEN** 追加的小节同样使用 CRLF，文件里不出现混用行尾

#### Scenario: 现有文件不合规时拒绝更新
- **WHEN** 现有 `SKILL.md` 缺少 frontmatter 必填项
- **THEN** 抛出 `SkillWriteError`，文件不被修改

### Requirement: 追加内容的归一化
更新项的正文 MUST 先经确定性归一化再落盘：剥掉模型可能多写的 YAML frontmatter 与一级标题；剩余正文 MUST 以二级标题开头，没有二级标题时 MUST 包进 `## {标题}（{当天} 补充）`；二级标题 MUST 带一次日期后缀，重复归一化 MUST 得到同一段文本。归一化后的文本 MUST 先过隐私与密钥门禁。

#### Scenario: 模型写了整份文件时只取增量
- **WHEN** 更新项的正文是一份带 frontmatter 与一级标题的完整 `SKILL.md`
- **THEN** 落盘的是剥掉 frontmatter 与一级标题后的小节，原文件不受影响

#### Scenario: 正文没有二级标题时补一个
- **WHEN** 更新项的正文没有二级标题
- **THEN** 落盘内容以 `## {标题}（{当天} 补充）` 开头，正文跟在标题之后

#### Scenario: 归一化是幂等的
- **WHEN** 对同一段更新内容归一化两次
- **THEN** 两次结果完全相同，日期后缀只出现一次

### Requirement: 现有 Skill 的只读检视
系统 SHALL 提供只读的现有 Skill 检视，返回：是否存在、是否合规、错误说明、行数、二级标题清单、行尾与全文。路径解析 MUST 严格位于技能根目录之内；围栏代码块里的标题 MUST 不计入标题清单；检视 MUST NOT 写入或修改任何文件。

#### Scenario: 检视一个合规的 Skill
- **WHEN** 检视一个 frontmatter 合规的 `SKILL.md`
- **THEN** 返回存在且合规、行数、二级标题清单、行尾与全文

#### Scenario: 检视不存在的 Skill
- **WHEN** 检视一个没有 `SKILL.md` 的目标
- **THEN** 返回不存在与错误说明，不抛异常、不写任何文件

#### Scenario: 围栏代码块里的标题不算小节
- **WHEN** 现有文件的代码块里出现二级标题
- **THEN** 该标题不计入标题清单

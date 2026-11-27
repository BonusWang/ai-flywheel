# knowledge-write-gate Specification Delta

## ADDED Requirements

### Requirement: Schema 四域路由解析与领域页路由校验
当挂载 Wiki 启用门禁且 `.wiki-schema.md` 声明了 `content_contract.deposit_routing` 时，系统 SHALL 动态解析各领域的触发口径、目标分区路径模板与允许页面类型，并在路由候选发现时扫描候选项目对应子目录下的既有页面基名作为只读 `domain_inventory`（排除隐藏文件与归档目录）。

系统 SHALL 对声明为项目领域页面类型（如 `project-algorithm`、`project-architecture`、`project-config`）的 `knowledge_doc` 计划项执行确定性路由与写前校验：
- 必须通过与经验卡同等的候选项目精确匹配与工作区/内容冲突校验；
- 目标相对路径必须匹配该页面类型在 `.wiki-schema.md` 中声明的分区与 `path_pattern`；
- 动作为 `update` 时目标文件必须已存在，且增量合并后必须保留原文件既有章节并在写前去除完全重复的内容（无新增实质内容时丢弃并留痕 `UPDATE_NO_NEW_CONTENT`）；
- 动作为 `create` 而目标文件已存在时，MUST 不得静默覆写原文件（应改标或拒绝并留痕）。

#### Scenario: 已有表结构页按增量更新通过校验
- **WHEN** 计划项声明路由至项目 alpha 的数据库结构页 `orders.md` 且 `action="update"`，原文件已存在且本次新增 1 个已证字段与 1 条坑
- **THEN** 路由校验与写前门禁通过，落盘后原文件已有字段完整保留且新字段已增量写入，并通过原子回读与 D2 后检

#### Scenario: 重复的增量内容被丢弃留痕
- **WHEN** 计划项对已有表结构页执行 `action="update"`，但其待补充条目均已存在于原文件中
- **THEN** 系统不修改磁盘文件，并记录 `UPDATE_NO_NEW_CONTENT`

### Requirement: 敏感项目配置页强制审批与凭据零落盘护栏
当 `knowledge_doc` 计划项的目标页面类型为 `project-config` 或目标路径命中敏感配置分区（或原文件 frontmatter 含 `sensitivity: private_sensitive`）时，`models.is_sensitive_write()` MUST 返回真，使其在审批门、Web 控制台与外部审批卡片上统一触发人工审批，不得在 `smart` 模式下自动静默落盘。

该类写入项在计划阶段与落盘阶段 MUST 继续经过 `SECRET_PATTERNS` 密钥检测：命中密码、Token 或私钥明文形态时 MUST 丢弃或阻断（`SECRET_LEAK_BLOCKED`），绝不落盘。

#### Scenario: 项目配置增量更新强制进入人工审批
- **WHEN** P03 规划对项目 alpha 的 `项目配置/database.md` 执行 `action="update"` 以补充逻辑库实例映射与连库注意事项（不含密码明文）
- **THEN** `is_sensitive_write()` 返回真，该计划项停在待审批状态等待维护者确认，不静默落盘

#### Scenario: 配置更新包含密码明文时被密钥门禁阻断
- **WHEN** 对项目配置页的更新内容包含未脱敏密码或密钥形态
- **THEN** 系统在写入前以 `SECRET_LEAK_BLOCKED` 丢弃或阻断该项，目标配置页字节不变

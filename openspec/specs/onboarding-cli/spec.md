# onboarding-cli Specification

## Purpose
TBD - created by archiving change feat-onboarding-cli-and-wiki-gate-neutral. Update Purpose after archive.
## Requirements
### Requirement: 只读环境诊断
`flywheel doctor` 对应一个诊断入口 `run_doctor(cfg)`，它 MUST NOT 修改任何文件、数据库或配置：不创建 data_dir 或其子目录，不创建或打开数据库写入，不保存 `flywheel.yaml`。诊断 MUST 覆盖以下七组检查，且每组以 `name`、`status`、`summary`、`detail` 四项报告：

- `config`：解析到的配置文件与 base_dir；没有配置文件时如实说明使用内置默认值；
- `storage`：`data_dir` 是否存在、是否可写；SQLite 数据库是否存在且文件头合法；派生的 `FLYWHEEL-CORE.md` 是否存在；尚不存在但首次运行会创建的路径 MUST 记为 `WARN` 而不是 `FAIL`；
- `knowledge_sink`：落点类型、根目录是否存在；类型为 `local_markdown_wiki` 且根目录不存在 MUST 记为 `FAIL`；根目录存在 `.wiki-schema.md` 时 MUST 检查写后钩子——`wiki_gate_enabled` 与 `wiki_gate_require_scripts` 为真而 `effective_postflight_script` 为空，或配置的脚本路径不存在，MUST 记为 `FAIL`；
- `skills`：`capabilities.skills_dirs` 中每个目录是否存在，以及发现的 `SKILL.md` 数量；未配置目录时 MUST 如实说明写入将落到默认技能目录；
- `runner`：以 `resolve_runner_mode(cfg)` 的解析结果为唯一口径；`runner.mode = api` 而 `base_url` 或 `model` 为空 MUST 记为 `FAIL`；`runner.mode = cli` 而命令为空或无法解析 MUST 记为 `FAIL`；模型执行安全阀关闭导致实际回落到 `dry_run` MUST 记为 `WARN`；
- `safety_valves`：有效审批模式、`workflow_enabled`、`model_execution_enabled`；`approval_mode = auto` 而 `safety_valves.auto_approve_writes = false` 时 MUST 明确写出已降级为 `smart`，并记为 `WARN`；
- `channels`：飞书通道关闭、仅文本、交互就绪三种状态；交互模式开启但缺少 bot 身份、私聊 `user_id` 或审批人，或 `lark-cli` 无法解析 MUST 记为 `FAIL`。

整体结论 `ok` MUST 为真当且仅当没有任何检查为 `FAIL`。

#### Scenario: 健康的默认配置
- **WHEN** 以全新的空目录为 base_dir 运行诊断
- **THEN** 结论 `ok` 为真；尚不存在的 data_dir、数据库与知识库根目录记为 `WARN`，不产生任何文件

#### Scenario: api 执行器缺 base_url
- **WHEN** 配置 `runner.mode = api`、`model` 非空而 `base_url` 为空
- **THEN** `runner` 检查为 `FAIL` 并点名 `base_url`，整体 `ok` 为假

#### Scenario: 契约根目录缺写后钩子
- **WHEN** 知识库根目录存在 `.wiki-schema.md`、`wiki_gate_require_scripts` 为真，而两个写后钩子配置名都为空
- **THEN** `knowledge_sink` 检查为 `FAIL`，并说明该根目录的写入会失败关闭

#### Scenario: 诊断不写盘
- **WHEN** 对任意配置运行诊断
- **THEN** 运行前后的文件树逐一相同，配置文件字节不变

### Requirement: 诊断输出与退出码
`flywheel doctor` MUST 在没有 `--json` 时输出人类可读的状态表（每项一行状态、名称与摘要，非 `PASS` 项附详情），在带 `--json` 时输出可被 `json.loads` 解析的完整报告；退出码 MUST 在 `ok` 为真时为 0，否则为 1。

#### Scenario: JSON 输出可解析
- **WHEN** 以 `--json` 运行诊断
- **THEN** 标准输出是合法 JSON，含整体结论、版本与七组检查

#### Scenario: 退出码跟随结论
- **WHEN** 任一检查为 `FAIL`
- **THEN** 命令退出码为 1；全部非 `FAIL` 时退出码为 0

### Requirement: 安全默认的客户端配置初始化
`flywheel init-client <client>` MUST 支持 `claude-code`、`codex`、`claude-desktop`、`antigravity` 四个客户端，接受 `--transport http|stdio`、`--apply`、`--target <path>` 与 `--json`。未给 `--apply` 时 MUST 为 dry-run：不写目标文件、不建父目录、不建备份，只输出解析出的目标路径、`ai-flywheel` 条目与合并摘要（目标是否存在、将保留的既有 server 与顶层键、是否替换同名条目）。目标路径 MUST 在未给 `--target` 时按该客户端在本平台的惯例位置解析。

条目内容 MUST 只依赖配置与传输方式：`http` 时为目标 `http://<server.host>:<server.port>/mcp` 的远程条目，`stdio` 时为 `flywheel mcp-stdio` 的本地条目。客户端不支持所请求的传输方式时（Claude Desktop 只支持 stdio）MUST 拒绝并给出可读错误，MUST NOT 写入任何文件。

#### Scenario: dry-run 零写入
- **WHEN** 目标文件与其父目录都不存在时运行不带 `--apply` 的初始化
- **THEN** 命令退出 0，输出条目与合并摘要，目标文件与父目录都不存在

#### Scenario: 渲染 http 条目
- **WHEN** 为 Claude Code 以 `--transport http` 渲染条目
- **THEN** 条目为目标 `http://127.0.0.1:8765/mcp` 的远程 MCP 条目

#### Scenario: 拒绝 Claude Desktop 的 http
- **WHEN** 为 Claude Desktop 指定 `--transport http`
- **THEN** 命令以非零退出，错误说明仅支持 stdio，不产生文件

### Requirement: 合并写入、备份与幂等
带 `--apply` 时，命令 MUST 先确保父目录存在；目标文件已存在且内容将变化时 MUST 先在其旁写备份（首选 `<target>.bak`，该备份已存在时改用带时间戳的备份名，MUST NOT 覆盖已有备份）。JSON 客户端 MUST 只合并 `mcpServers["ai-flywheel"]`，TOML 客户端 MUST 只更新或追加 `[mcp_servers.ai-flywheel]`（带引号的写法视为同一条目并就地更新，MUST NOT 产生重复段）；其余顶层键、同级 server 与其它段 MUST 保持原值。写入 MUST 经同目录临时文件原子替换，行尾 MUST 为 LF。TOML 结果 MUST 在替换目标文件前用 `tomllib.loads` 重新解析并通过条目与其余段的一致性核对；JSON 结果同样 MUST 重新解析并核对。合并后的文本与现有内容完全相同时 MUST 视为无变化：不写文件、不产生备份。`--apply` 重复执行 MUST NOT 产生重复条目或重复段。

#### Scenario: JSON 合并保留兄弟 server
- **WHEN** 目标 JSON 已含其它 MCP server 与顶层键，`--apply` 合入 ai-flywheel 条目
- **THEN** 其它 server 与顶层键原值保留，`ai-flywheel` 条目为所渲染条目，备份保存合并前的完整内容

#### Scenario: TOML 合并通过重校验
- **WHEN** 目标 TOML 含其它 `[mcp_servers.*]` 段与其它顶层段，`--apply` 合入 ai-flywheel 段
- **THEN** 写后文件可被 `tomllib.loads` 解析，其它段原值保留，`ai-flywheel` 段为所渲染条目

#### Scenario: 二次 apply 零变化
- **WHEN** 对同一目标连续执行两次 `--apply`
- **THEN** 第二次不修改文件、不产生新备份，条目与段均只有一份

### Requirement: 解析失败时失败关闭
目标文件已存在但无法按声明的格式解析（非法 JSON、非法 TOML、顶层不是对象、`mcpServers` 不是对象）时，命令 MUST 失败关闭：以非零退出，给出可读错误，目标文件字节不变，且 MUST NOT 创建备份。

#### Scenario: 非法 JSON
- **WHEN** 目标文件内容不是合法 JSON 且带 `--apply`
- **THEN** 命令退出非零并说明解析失败，文件字节不变，无备份产生

#### Scenario: 非法 TOML
- **WHEN** 目标文件内容不是合法 TOML 且带 `--apply`
- **THEN** 命令退出非零并说明解析失败，文件字节不变，无备份产生


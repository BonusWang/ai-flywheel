# Proposal: 接入自检 CLI 与知识写入门禁去 Wiki 化

## Why

三处现象都指向同一个缺口：**配置是否可用，只有跑到真实批次里才知道**。

1. **没有只读的自检入口。** `runner.mode = api` 却漏了 `base_url`、`local_markdown_wiki` 根目录不存在、根目录有 `.wiki-schema.md` 但 `wiki_gate_require_scripts` 为真而后检脚本没配——这些都不是语法错误，`load_config` 全部照收。它们要到一次真实运行里才以「dry_run 悄悄顶替」「P04 写入失败回滚」的形式暴露，原因码埋在运行记录里。首次接入的用户没有一条「先体检、再开跑」的路径。
2. **客户端接入靠人肉复制与手改。** 门户 Connect 页给出的是片段，用户仍要自己找到 Claude Code / Codex / Claude Desktop / Antigravity 的配置文件，把片段合并进已有 JSON / TOML。手工合并的真实风险是覆盖别人的 MCP server、写坏整个配置文件、重复添加同一条目——而这份配置往往同时承载用户其它工具。
3. **写后钩子的配置名把门禁绑在 Wiki 上。** Schema 写入门禁（`WikiGate`）是契约驱动的：根目录有 `.wiki-schema.md` 就适用，与知识库是不是 Wiki 无关。但它的写后钩子只有 `wiki_d2_postflight_script` 一个名字，于是任何使用同一套契约的非 Wiki 知识库（通用 Markdown 知识库、团队文档库）都得沿用 Wiki 术语来描述自己的索引/校验脚本。缺的不是重命名，而是一个中性名，且**旧名必须 100% 继续可用**。

## What Changes

- **`flywheel doctor`**（新增 `backend/ai_flywheel/core/doctor.py`，`cli.py` 增加子命令）：
  - `run_doctor(cfg) -> dict`，**只读**：不建目录、不建/不改数据库、不写配置；
  - 七组检查：`config`（配置来源与 base_dir）、`storage`（data_dir 存在与可写、SQLite 头、派生 `FLYWHEEL-CORE.md`）、`knowledge_sink`（类型、根目录、`.wiki-schema.md`、必需的后检钩子）、`skills`（配置目录与 `SKILL.md` 计数）、`runner`（`resolve_runner_mode` 与 `api` / `cli` 的必要条件）、`safety_valves`（有效审批模式，明确写出 `auto → smart` 降级、 workflow 与模型执行阀）、`channels`（飞书：关闭 / 仅文本 / 交互就绪）；
  - 每项报告 `name` / `status`（`PASS` | `WARN` | `FAIL`）/ `summary` / `detail`；整体 `ok` 为真当且仅当零 `FAIL`；
  - `flywheel doctor [--json]`：表格或 JSON，`ok` 为真退出 0，否则退出 1。
- **`flywheel init-client`**（新增 `backend/ai_flywheel/core/onboarding.py`，`cli.py` 增加子命令）：
  - `flywheel init-client <client> [--transport http|stdio] [--apply] [--target <path>] [--json]`，支持 `claude-code`、`codex`、`claude-desktop`、`antigravity`；
  - 默认 **dry-run**，零磁盘写入（连父目录都不建），输出目标路径、`ai-flywheel` 条目与合并摘要（将保留的既有 server 与顶层键）；
  - `--apply`：确保父目录存在；目标已存在且内容将变化时先写备份 `<target>.bak`（已存在则用带时间戳的 `.bak`），JSON 客户端只合并 `mcpServers["ai-flywheel"]`、TOML 客户端只更新或追加 `[mcp_servers.ai-flywheel]`（含带引号写法），其余键 / server / 段原样保留；临时文件 + 原子替换 + LF；TOML 写后用 `tomllib.loads` 重新校验；二次 `--apply` 零变化、零重复条目。
- **写后钩子中性名**：`KnowledgeSinkConfig` 新增 `postflight_hook_script`（环境变量 `FLYWHEEL_POSTFLIGHT_HOOK_SCRIPT`）与只读属性 `effective_postflight_script`（中性名优先，回落 `wiki_d2_postflight_script`）；`adapters/wiki/postflight.py` 的三处读取全部改为有效值，`wiki_d2_postflight_script` 语义与 `FLYWHEEL_WIKI_D2_POSTFLIGHT` 完全不变；`flywheel.yaml.example` 同时文档化两个名字。
- **测试**：新增 `tests/test_onboarding_cli.py`（doctor 只读与退出码、init-client 合并/备份/幂等/失败关闭、别名优先级与环境变量、写后钩子真实调用）。

## Capabilities

### New Capabilities

- `onboarding-cli`：`flywheel doctor` 的只读诊断契约（检查项、状态、整体 ok、退出码）与 `flywheel init-client` 的安全默认初始化契约（dry-run 零写入、按客户端渲染条目、合并写入、备份、原子 LF、幂等、失败关闭）。

### Modified Capabilities

- `knowledge-write-gate`：新增「写后钩子的中性配置名与遗留别名」一条增量要求；写前门禁、原子写入、回滚、索引同步等既有行为不变。

## Impact

- 代码新增：`backend/ai_flywheel/core/doctor.py`、`backend/ai_flywheel/core/onboarding.py`、`tests/test_onboarding_cli.py`。
- 代码修改：`backend/ai_flywheel/config.py`（一个字段、一个属性、一个环境变量映射）、`backend/ai_flywheel/adapters/wiki/postflight.py`（三处读取改有效值，错误信息文案点名两个配置名）、`backend/ai_flywheel/cli.py`（两个子命令）。
- 配置：`flywheel.yaml.example` 的 `knowledge_sink` 文档化 `postflight_hook_script`，并注明 `wiki_d2_postflight_script` 是兼容别名。
- 兼容性：既有 `flywheel.yaml`、`FLYWHEEL_WIKI_D2_POSTFLIGHT`、`.wiki-schema.md` 契约与写入链路行为不变；新增字段默认空，不改变任何现有配置的语义。
- 不改变：P00–P07 阶段副作用边界、门户 API 与前端契约、`resolve_runner_mode` 与安全阀语义、`is_sensitive_write` 判定。
- 约定与风险：`init-client` 的默认路径是各客户端的惯例位置（Claude Desktop 随平台），`--target` 始终可覆盖；JSON 客户端在合并时按两空格缩进重排（语义保留、格式归一），TOML 客户端按段替换、其余行原样保留。
- 不在范围：写入后自动重启客户端、`claude mcp add` 之类的官方 CLI 调用、doctor 的自动修复、`WikiGate` 类改名。

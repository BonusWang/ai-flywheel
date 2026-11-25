# Design: 接入自检 CLI 与知识写入门禁去 Wiki 化

## 决策 1：诊断是一个纯函数 `run_doctor(cfg)`，不装配运行时

- **备选方案 A：`FlywheelRuntime.create(...)` 后读运行时对象。**
  - 缺点：运行时构造会建 data_dir、建数据库、重建全文索引与核心卡——诊断本身就成了写操作，与「只读」要求直接冲突。
- **采用方案 B：`run_doctor(cfg: FlywheelConfig)` 只读配置与文件系统。**
  - 优点：只读由构造保证，不依赖调用方自律；测试可以直接断言「运行前后文件树一致」；CLI 只负责加载配置与打印。
  - 取舍：诊断看不到运行中的进程内部状态（监听线程、调度器）。这些不属于「配置能否跑」的问题，留给门户。

## 决策 2：三档状态，`WARN` 与 `FAIL` 的分界是「首次运行会不会自愈」

- `WARN`：当前不可用，但首次 `serve` / 首次写入会创建或自愈——data_dir 不存在、数据库未创建、`builtin_kb` 根目录未创建、知识库根目录有 schema 而脚本可选且未配。
- `FAIL`：照现状运行会做错事或失败——`api` 执行器缺 `base_url` / `model`（会静默落到 dry_run）、`cli` 命令解析不到、挂载的知识库根目录不存在、要求写后钩子而没配（写入必然失败关闭）、交互式飞书缺审批人、数据库文件不是 SQLite。
- 整体 `ok` 只看 `FAIL`：`WARN` 是「第一次跑就知道了」，不该让 CI 或首次接入被判红。
- **探针只做无副作用读取**：目录可写用 `os.access`；数据库用文件头 `SQLite format 3\0`，绝不用 `sqlite3.connect`（连接会创建文件）；0 字节数据库视为「首次写入时初始化」。

## 决策 3：`init-client` 默认 dry-run，合并而非重写

- **备选方案 A：直接生成整份配置文件。**
  - 缺点：目标文件往往同时承载用户其它 MCP server、主题、项目列表；整份生成等于清空用户配置。
- **采用方案 B：只合并 `ai-flywheel` 条目，默认不落盘。**
  - dry-run 是默认值：先看预览再决定是否 `--apply`；dry-run 连父目录都不建，避免「先建目录再失败」的半成品。
  - `--apply` 的固定顺序：读 → 解析（失败即停）→ 合并 → 重新解析并核对 → 备份 → 临时文件原子替换 → 读回比对。任一步失败，目标文件未被触碰。

## 决策 4：JSON 重排、TOML 按段替换——两种格式各自的可行做法

- **JSON**：`json.loads` 语义保留但会丢弃重复键、统一格式，所以合并后按两空格缩进重写整份文件。取舍：未改动的条目只保留语义、不保留原始空白与缩进；换来的是绝对可解析与键序稳定（顶层键与同级 server 顺序保留）。核对项：重解析后 `ai-flywheel` 条目等于所渲染条目，其余顶层键与同级 server 与原文件逐一相等。
- **TOML**：Python 标准库只有 `tomllib`（只读），没有写回器；完整重写需要引入第三方依赖。因此采用**文本级段替换**：
  - 用行首正则匹配 `[mcp_servers.ai-flywheel]`、`[mcp_servers."ai-flywheel"]`、`[mcp_servers.'ai-flywheel']` 三种写法，替换该段到下一个表头之前的内容；找不到段则追加到文件末尾；
  - 写前用 `tomllib.loads` 解析原文件（非法即失败关闭），写后再解析一次，并核对：条目键值等于所渲染条目、其余顶层段与同级 `mcp_servers` 条目逐一相等；
  - 取舍：不重排用户的其余行（这正是「保留其它段」的最强形式）；若原文件把该条目写成内联表，段替换会与内联定义冲突，重校验会失败并**不写文件**，错误信息提示手工修改。

## 决策 5：幂等与备份的边界

- 合并结果与现有内容逐字节相同时：不写、不备份、`action=unchanged`。这让「重复执行 `--apply`」在文件系统上真正零变化，也让幂等可测（字节相等 + 备份数量不变）。
- 备份只在「目标已存在且内容将变化」时产生：首选 `<target>.bak`；若已存在，改用 `<target>.<UTC 时间戳>.bak`（同秒冲突再加序号），**不覆盖**任何已有备份——第一次的原始备份永远是最干净的还原点。

## 决策 6：中性名做成计算属性，不引入第二份存储

- **备选方案 A：加载配置时把遗留值搬进新字段（迁移）。**
  - 缺点：配置一旦被 `save_config` 写回，用户的遗留键会消失或双写；两处值可能漂移。
- **采用方案 B：`effective_postflight_script` 计算属性**：`postflight_hook_script.strip() or wiki_d2_postflight_script.strip()`。
  - 没有迁移、没有双写、没有漂移；两个环境变量各自映射到自己的字段，优先级由同一行代码决定。
  - `adapters/wiki/postflight.py` 的三处读取（写后执行、回滚重同步、脚本缺失判定）全部改读有效值，`WikiGate` 与知识落点无需改动——它们只委托，不直接读字段。

## 各客户端的目标路径与传输方式

| 客户端 | 格式 | 默认传输 | 惯例路径（`--target` 可覆盖） |
|---|---|---|---|
| `claude-code` | JSON | http | `~/.claude.json` |
| `codex` | TOML | http | `~/.codex/config.toml` |
| `claude-desktop` | JSON | stdio | Windows `%APPDATA%\Claude\claude_desktop_config.json`；macOS `~/Library/Application Support/Claude/…`；Linux `~/.config/Claude/…` |
| `antigravity` | JSON | http | `~/.gemini/antigravity/mcp_config.json` |

路径是惯例而非契约：`--target` 始终优先。Claude Desktop 只接受 stdio，请求 http 时拒绝而不是写出一份它读不懂的配置。

## 验证策略

- **自动测试（`test`）**：`tests/test_onboarding_cli.py` 20 项——doctor 七组检查的结构与只读（文件树前后一致）、api 缺 `base_url` 与缺写后钩子的 `FAIL` 与退出码、JSON 输出可解析、auto→smart 降级提示、会话式飞书缺审批人的 `FAIL`；init-client dry-run 零写入（含父目录）、JSON/TOML 合并保留兄弟条目与备份、二次 apply 零变化、引号段就地更新、坏 JSON / 坏 TOML / 错误 `mcpServers` 形状失败关闭且无备份、Claude Desktop 拒绝 http；别名优先级、回落、环境变量；`run_postflight` 以真实 bash 子进程分别用中性名与遗留名跑通，缺脚本时报 `D2_SCRIPT_MISSING`。
- **真实 CLI（`real`）**：在仓库内以 `.venv` 解释器执行 `python -m ai_flywheel.cli doctor --json` 与 `python -m ai_flywheel.cli init-client claude-code --json`（纯 dry-run，不写任何客户端配置），记录输出与退出码。
- **一键闸门（`test`）**：`openspec validate --all --strict` 与 `python scripts/verify_repo.py` 全绿。
- **不在本次验证范围**：以真实配置向知识库写入一次并观察写后钩子。别名是纯读取口径的改动，钩子层的真实子进程测试已覆盖读取路径；完整 P04 写入需要维护者授权的真实批次，另行安排。

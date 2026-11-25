# AI Flywheel

> **以目标开局，以证据执行，以复盘提炼，以沉淀复用，以再验证迭代**
>
> *经验 · 证据 · 分歧 · 蒸馏 · 复用 — 缺一不算真飞轮*

**AI Flywheel** 是一个**本地优先、策略与知识库彻底解耦、面向多 AI 工具的证据驱动协作飞轮产品**。它通过标准 **MCP** 与可选极薄转发 Hook 接入 **Codex、Claude Code、Claude Desktop、Antigravity** 等主流 AI 工具，后台自主运转 **P00–P07 固定 8 节点工作流**，并提供极简 `5+1` 控制台（含可视化「自动化任务」调度控制面）、自动派生核心索引卡与周五分层蒸馏机制。

---

## 核心特性（v0.4）

1. **方法论策略与知识库彻底解耦**
   - 反偷懒执行契约（`baseline` / `execution`）、8 步闭环、死循环保护阈值、证据分级（`real / test / static / unknown`）及个人协作偏好由飞轮独立托管（`profile/policies/`），无需绑定外部 Wiki。
   - 知识库作为可插拔落点（默认内置 `./data/knowledge` 开箱即用，也可挂载本地 Markdown / Obsidian Wiki 并启用 Schema 写入门禁）。
2. **两级会话初筛选器（过滤临时闲聊，只沉淀高价值会话）**
   - **一级规则硬筛（0 Token）**：扫盘自动过滤 `agent-*` / `subagent-*` 内部碎片与极短/空会话；自动解析 JSONL 真实 `workspace` 并剥离系统提示词包装；正式项目/知识目录高优放行，临时聊天目录（`*Chat`）走灰名单（需足够深度与知识/工程信号），手工/MCP 显式录入豁免。
   - **二级 AI 语义价值闸门（P02）**：授权模型对一次性翻译、闲聊、简单查命令直接判定 `worthy: false` 跳过归档，严禁硬凑笔记。
3. **双轨错题观察池 + 分歧反例保留（防规则膨胀 & 补齐五判据）**
   - 失误按 `red_silent`（🔴 沉默错误）/ `yellow_caught`（🟡 自查拦截）/ `green_minor`（🟢 轻微）分级入池；普通失误按 `root_cause_key` 聚类满 **3 次（事不过三）** 才晋升规则，高危沉默错误可首次直通试运行。
   - 规则复测失败或新会话出现例外时，自动记录 `dissent_records`（保留反例与例外条件，绝不静默覆盖原规则），并在下次预检（`preflight_activate`）与规则卡片中一并提醒。
4. **写时隐私门禁 + 经验卡时效保鲜（防记忆投毒）**
   - 所有知识文档、Skill 与规则写入前均经过隐私门：自动脱敏 `<private>...</private>` 区块，硬拦截 API Key / Token / 私钥泄露（`SecretLeakError`）。
   - 经验卡按 `freshness_tier`（`volatile` 30 天 / `evolving` 90 天 / `evergreen` 180 天）管理有效期；过期卡在检索与核心索引卡中自动降权并标注 `⚠️ [已过复核期，执行前请先核实]`；在真实任务中通过 `real` 验证并传回 `used_knowledge_paths` 时自动续期。
5. **派生核心索引卡 + 周五分层蒸馏 + 反 Skill 表演**
   - 自动在 `data/FLYWHEEL-CORE.md` 派生 ≤200 行 / ≤24 KB 的跨智能体只读索引卡，并通过 MCP Resource `flywheel://core-card` 暴露给任意智能体。
   - 每周五 20:00 自动执行分层蒸馏：14 天零命中试运行规则自动转入 `dormant`（可一键唤醒，全程审计留痕）；标记超期经验卡；检测触发词高重合规则、21 天零调用的闲置 Skill 与连续失败 Skill（`used_skills` 实效账本），通过门户与飞书决策卡推送精简建议；保存每周成长指标快照（样本 `< 5` 条显式提示）。
6. **合同驱动的 Wiki 路由 + 反静默回退契约（模型说了什么，就写什么）**
   - 挂载带 `.wiki-schema.md` 的 Wiki 时，P03 从 Wiki 现场发现候选页面类型与项目目录，把候选集交给模型**声明**路由（`page_type` / `project` / `reason`），再由工作区与草稿证据**校验**该声明：声明不在候选集、声明无归属或与证据冲突的笔记**不写入**，只记 `planned_doc_unrouted` 审计事件——归属永远不猜。
   - `api` / `cli` 模式下，模型给出的写入计划或会话审查不可用时，该节点**直接失败**并留下原因码与结构化诊断，绝不偷偷换成本地启发式却仍报 `api:<model>`；P02 与 P03 的来源（API 模型 / 本地 CLI / 本地启发式）在节点摘要与载荷里写明。
   - P02 只产出候选：错题与分歧留在节点载荷里，审批通过后才在 P04 入库；整批只重建一次全文索引。
7. **Skill 追加式无损更新 + 控制台并排预览（不重写，只追加）**
   - `action="update"` 的语义是「在现有 `SKILL.md` 末尾追加一节」：旧文件的每一个字节都是新文件的前缀，写前备份、原子替换、回读比对 SHA-256，并把原有行尾（CRLF 仍是 CRLF）保持不变。
   - 控制台提供只读预览接口 `GET /api/site/skills/{name}/preview`（现有文件大小、H2 大纲与全文）；审批卡在更新项上直接展示已有大纲，并提供「与现有文件对比」的展开面板——**写进去的和预览的逐字一致**。
   - 零配置 `dry_run` 计划不再生成模板化的 `SKILL.md`；重复流程只作为候选上报。
8. **模型调用可靠性 + 原子审批 + 崩溃恢复（可观测、可恢复）**
   - `runner.api.reasoning_effort` 可选（留空即不发送该参数，非空时进入缓存键）；`runner.api.transport_retries` 对超时与连接错误**立即重试且不退避**（超时不是限流），HTTP 状态错误不重试且只报状态码、不带网关地址。
   - 每次调用都留痕：尝试次数、耗时、实际服务模型、prompt / completion / reasoning token，用量随 P02 与 P03 的节点载荷走；模型回复**通过语义校验之后**才写缓存，坏输出不会污染下一批。
   - 审批认领是原子的：认领、状态迁移、已批条目、决策卡与审计事件在同一个写事务里落盘，并发审批或「批准 / 取消」竞态只有一个赢家；执行中的批次每 15 秒续锁并打心跳，`serve` 启动时与每 30 秒收割心跳停止的批次（下游 `SKIPPED`、会话回到 `pending`、决策卡置为失败）。被拒绝的批次终态是 `CANCELLED`，与「做了一半」的 `PARTIAL` 严格区分。
9. **智能审批、Skill 封装写入与多端闭环**
   - 支持 `smart`（常规笔记与试运行规则自动放行，Skill 变更 / 高风险 / 超批量自动挂起审批 + 飞书 CLI 通知）、`always`、`auto` 三种审批模式；混合批次提供「仅同意常规项」。安全阀 `safety_valves.auto_approve_writes = False` 拥有最终否决权，会把 `approval_mode = auto` 降级为 `smart` 而不是绕过审批。
   - 飞书交互审批（可选）：挂起时发送带按钮的卡片，在手机上即可批准或取消，执行结果回到同一张卡；链路未验证前一律退回文本通知（见「飞书交互审批」一节）。
   - 标准 MCP Server（`Streamable HTTP /mcp` + `stdio`），暴露 6 个核心 Tool + 4 个只读 Resource（含 `flywheel://core-card`）与 `/llms.txt`。
   - 极简 `5+1` 控制台（`每日成果` / `每日沉淀` / `自动化任务` / `规则与规矩` / `接入与设置` + `关于`），提供 GitLab-CI 风格 8 节点流水线批次视图、上海时区定时调度热管理（可视化编辑 Cron / 启停 / 守护巡检与执行记录）及全链路审计日志。

---

## 快速开始（3 分钟本地启动）

### 1. 环境要求
- Python `3.11+`
- Node.js `20+`（仅构建前端静态资源需要）

### 2. 安装与构建

```bash
git clone https://github.com/BonusWang/ai-flywheel.git
cd ai-flywheel

python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -e ".[dev]"

cd frontend
npm install
npm run build
cd ..
```

### 3. 一键启动

```bash
flywheel serve --seed
```

启动后访问：
- **飞轮控制台**：`http://127.0.0.1:8765`
- **MCP Streamable HTTP 端点**：`http://127.0.0.1:8765/mcp`
- **Agent 发现文档**：`http://127.0.0.1:8765/llms.txt`

如需以 `stdio` 模式接入 Claude Desktop 等客户端：

```bash
flywheel mcp-stdio
```

---

## 配置与外部挂载

复制 `flywheel.yaml.example` 为 `flywheel.yaml`（已被 `.gitignore` 忽略），即可无侵入挂载私有配置覆盖包、Skills 目录、本地知识库、OpenAI 兼容 API 网关及飞书通知通道：

```yaml
profile:
  override_dir: "/path/to/your-home-repo/flywheel-profile"  # 可选：私有偏好与规则覆盖包

capabilities:
  skills_dirs:
    - "/path/to/your-skills"                                # 可选：本地 SKILL.md 目录

knowledge_sink:
  type: "local_markdown_wiki"                               # builtin_kb | local_markdown_wiki
  root: "/path/to/your-wiki"                                # 挂载任意本地 Markdown / Obsidian 目录
  # 挂载带 .wiki-schema.md 的 Wiki 时，P03 按合同路由知识文档：模型从 Wiki 动态发现的候选集里声明页面类型与项目；
  # 声明不在候选集、声明无归属或与工作区/草稿证据冲突时不写入，记入 planned_doc_unrouted 审计事件。

workflow:
  approval_mode: "smart"                                    # smart | always | auto
  friday_weekly_enabled: true

runner:
  mode: "api"                                               # dry_run | api | cli
  api:
    base_url: "http://127.0.0.1:8000/v1"                     # 任意 OpenAI 兼容端点
    model: "your-model"                                     # 端点提供的模型名
    timeout_seconds: 240                                    # 单次调用的墙钟上限（秒）
    reasoning_effort: ""                                    # 留空则不发送该参数；非空时进入缓存键
    transport_retries: 1                                    # 超时/连接错误的即时重试次数（0 关闭）

safety_valves:
  # 安全阀拥有最终否决权：auto_approve_writes 为 false 时，approval_mode: auto 会被降级为 smart，
  # 而不是绕过审批；model_execution_enabled 关闭后，所有会调用模型或本地 CLI 的执行器都退回 dry_run。
  auto_approve_writes: false
  model_execution_enabled: null                             # 留空则沿用旧的 cli_execution_enabled
```

> 完整字段与逐项注释见 [`flywheel.yaml.example`](./flywheel.yaml.example)；本仓是公开仓库，
> 示例里不写任何个人绝对路径——私有配置只进被 `.gitignore` 忽略的 `flywheel.yaml`。

### 飞书交互审批（可选）

在飞书卡片上直接批准 / 仅批准常规项 / 取消一次沉淀批次，结果回到同一张卡：

```yaml
delivery:
  feishu_cli:
    enabled: true
    command: "lark-cli"        # 需要 lark-cli >= 1.0.64（本机 npm 安装即可）
    identity: "bot"
    user_id: "ou_xxx"          # 私聊接收人（open id）；交互卡片只发私聊
    notify_on_fyi: false
    interactive: true           # 打开交互审批
    approver_open_ids: []       # 额外审批人（默认 user_id 本人可批）
```

首次启用三步：

1. 在飞书开发者后台为 lark-cli 绑定的应用订阅「卡片回传交互」（`card.action.trigger`，长连接方式），并发布一个版本——平台侧没有预检，只能靠下一步验证。
2. 重启 `flywheel serve`（`command` / `command_args` / `identity` 改动都需要重启生效）。
3. 运行 `flywheel feishu-check` 并在收到的测试卡上点一下；通过之前，所有审批通知都是纯文本，不会出现点了没反应的按钮。

说明与边界：

- 自检是闸门：lark-cli 版本或文件、审批人、接收人任一变化都会使自检失效并退回文本，重新运行 `flywheel feishu-check` 即可恢复。
- 审批卡按摘录批准：敏感情项（Skill 变更 / 删除 / 覆盖核心文件）展示动作、路径、理由、正文摘录与内容校验哈希，并注明「摘录不等于全文」；全文始终可在门户逐项核对。
- 链路不可信时自动降级：`serve` 未运行、监听重连中、自检失效或交互卡连续发送失败，审批通知退回纯文本并写明原因；已送达的卡片会把按钮收掉、提示到电脑前处理。
- 同一个飞书应用的卡片点击只允许一个监听者：不要在两台机器上同时开交互模式。
- 点击校验：只有配置的审批人、在送达的那条消息上、对这张卡提供的选项的点击才生效；重复点击与门户并发作答只有一次生效（见 `fix-approval-claim-and-recovery` 的原子认领）。

详见 [CHANGELOG.md](./CHANGELOG.md) 了解各版本演进与 SQLite 自动迁移说明。

---

## 开发流程（OpenSpec 规格驱动）

本仓库用 [OpenSpec](https://github.com/Fission-AI/OpenSpec) 管理行为契约与变更，约定见 `openspec/config.yaml`（生成产物时自动注入）：

- `openspec/specs/`：现行基线规格。首轮已建 `knowledge-write-gate`（知识文档受控写入与 Wiki 门禁）与 `workflow-orchestration`（P00–P07 编排）；其余能力在首次触及时补基线。
- `openspec/changes/`：进行中的变更（proposal → specs → design → tasks）与 `archive/` 已归档变更。
- 流程：Explore 澄清 → Propose 产出四类产物 → Apply 实现 → Verify（test / real 两级证据）→ 确认后 Archive。

```bash
openspec new change fix-<kebab-case-name>   # 新建变更（修复 fix- / 新能力 feat- / 治理 chore-）
openspec status --change <name>             # 产物完成度
openspec validate --all --strict            # 规格校验
```

代码图谱：在仓库根执行 `codegraph init` 建立本机索引（`.codegraph/` 不入库）；改动前用 `codegraph callers|impact <symbol>` 看影响面，改动后 `codegraph sync`。

## 验收与自动化测试

一条命令跑完全部验收——公开仓卫生、CHANGELOG 标题检查、规格校验、测试套件（可选前端构建）：

```bash
python scripts/verify_repo.py --frontend
```

它只在有检查失败时返回非零，**不会自动修任何东西**：失败就是一份报告。也可以单独跑其中几项：

```bash
pytest tests -q                              # 测试套件
openspec validate --all --strict             # 规格校验
cd frontend && npm run build                 # 前端产物
```

跨层不变量（版本四处一致、前端状态联合 ↔ 后端枚举、敏感情项判定、提示词骨架、`serve` 角色冒烟、Windows 契约、公开仓卫生）固化在 [`tests/test_arch_invariants.py`](./tests/test_arch_invariants.py)；
架构地图、P00–P07 副作用矩阵与不可协商的护栏见 [`AGENTS.md`](./AGENTS.md)。

## License

[MIT](./LICENSE)

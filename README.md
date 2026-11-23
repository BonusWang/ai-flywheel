# AI Flywheel

> **以目标开局，以证据执行，以复盘提炼，以沉淀复用，以再验证迭代**
>
> *经验 · 证据 · 分歧 · 蒸馏 · 复用 — 缺一不算真飞轮*

**AI Flywheel** 是一个**本地优先、策略与知识库彻底解耦、面向多 AI 工具的证据驱动协作飞轮产品**。它通过标准 **MCP** 与可选极薄转发 Hook 接入 **Codex、Claude Code、Claude Desktop、Antigravity** 等主流 AI 工具，后台自主运转 **P00–P07 固定 8 节点工作流**，并提供极简 `4+1` 控制台、自动派生核心索引卡与周五分层蒸馏机制。

---

## 核心特性（v0.3）

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
6. **智能审批、Skill 封装写入与多端闭环**
   - 支持 `smart`（常规笔记与试运行规则自动放行，Skill 变更 / 高风险 / 超批量自动挂起审批 + 飞书 CLI 通知）、`always`、`auto` 三种审批模式。
   - 标准 MCP Server（`Streamable HTTP /mcp` + `stdio`），暴露 6 个核心 Tool + 4 个只读 Resource（含 `flywheel://core-card`）与 `/llms.txt`。
   - 极简 `4+1` 控制台（`每日成果` / `每日沉淀` / `规则与规矩` / `接入与设置` + `关于`），GitLab-CI 风格 8 节点流水线批次视图与全链路审计日志。

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

workflow:
  approval_mode: "smart"                                    # smart | always | auto
  friday_weekly_enabled: true

runner:
  mode: "api"                                               # dry_run | api | cli
  api:
    base_url: "http://127.0.0.1:3425/v1"
    model: "group/cheap-first"
    timeout_seconds: 240
```

详见 [CHANGELOG.md](./CHANGELOG.md) 了解各版本演进与 SQLite 自动迁移说明。

---

## 自动化测试

```bash
pytest tests/test_flywheel_core.py tests/test_v2_schema_and_skills.py tests/test_v3_true_flywheel.py -v
```

## License

[MIT](./LICENSE)

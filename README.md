# AI Flywheel

> **以目标开局，以证据执行，以复盘提炼，以沉淀复用，以再验证迭代**
>
> *正确执行 → 证实结果 → 提炼规律 → 改变下次行为*

**AI Flywheel** 是一个**本地优先、策略与知识库彻底解耦、面向多 AI 工具的证据驱动协作飞轮产品**。它通过标准 **MCP** 与可选极薄转发 Hook 接入 **Codex、Claude Code、Claude Desktop** 等主流 AI 工具，后台自主运转 **P00–P07 固定 8 节点工作流**，并提供一个高可读性的个人站与实时工作流可视化控制台。

---

## 核心特性

1. **方法论策略与知识库彻底解耦**
   - 反偷懒执行契约（`baseline` / `execution`）、8 步闭环、死循环保护阈值、证据分级（`real / test / static / unknown`）以及个人协作偏好全部由飞轮服务独立托管（`profile/policies/`），无需绑定任何外部 Wiki。
   - 知识库作为可插拔落点（默认内置 `./data/knowledge` 开箱即用，也可一键挂载 Obsidian / 本地 Markdown Wiki）。
2. **三层可复刻架构（借鉴 [AIHOT](https://github.com/KKKKhazix/AIHOT)）**
   - `profile/` 可复刻配置包 + 可选 `profile.override_dir`（如私有 Home Repo），开源仓库零隐私硬编码。
   - `publication/` 统一读取层：Web 个人站、REST API、MCP Resources 与 `/llms.txt` 共享单一数据源，支持 `flywheel export-public` 脱敏导出。
   - 显式安全阀（`WORKFLOW_ENABLED`、`CLI_EXECUTION_ENABLED`、`AUTO_APPROVE_WRITES`、`GIT_CLOSURE_ENABLED`）。
3. **跨 AI 工具真闭环（MCP + 极薄 Hook + P00 扫盘兜底）**
   - 标准 MCP Server（同时支持 `Streamable HTTP /mcp` 与 `stdio`），暴露 6 个轻量 Tool + 3 个只读 Resource。
   - **Codex / Claude Code**：提供 1 行零业务逻辑的 fail-open 转发 Hook，实现 100% 强制开局激活与死循环保护。
   - **Claude Desktop**：提供 `claude_desktop_config.json` 与 20 行全局指令模板，支持直推会话摘要与结果回执。
4. **P00–P07 自主工作流与两级规则激活状态机**
   - 默认在 P03 后进入 `AWAITING_APPROVAL`，在 Web 可视化画布预览写入 Diff 与规则草案后一键批准进入 P04–P07（也支持切换全自动写入）。
   - 任何节点失败触发 `fail-run`，保留已完成证据并强制安全释放独占锁。
   - 沉淀规则按两级生命周期演进：`unverified_active` → 自然任务真实验证通过晋升 `verified_active` → 复发自动降级 `needs_revision`。

---

## 快速开始（3 分钟本地启动）

### 1. 环境要求
- Python `3.11+`
- Node.js `20+`（仅首次构建前端静态资源需要）

### 2. 安装与构建

```bash
git clone https://github.com/BonusWang/ai-flywheel.git
cd ai-flywheel

# 创建虚拟环境并安装后端
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -e ".[dev]"

# 构建前端可视化个人站
cd frontend
npm install
npm run build
cd ..
```

### 3. 一键启动（带内置演示数据）

```bash
flywheel serve --seed
```

启动后访问：
- **飞轮个人站与可视化画布**：`http://127.0.0.1:8765`
- **MCP Streamable HTTP 端点**：`http://127.0.0.1:8765/mcp`
- **Agent 发现文档**：`http://127.0.0.1:8765/llms.txt`

如需以 `stdio` 模式接入 Claude Desktop 等客户端：

```bash
flywheel mcp-stdio
```

---

## 配置与三体协作（Home Repo + Wiki + AI Flywheel）

复制 `flywheel.yaml.example` 为 `flywheel.yaml`（已被 `.gitignore` 忽略），即可无侵入挂载你自己的私有配置、Skills 目录和本地知识库：

```yaml
profile:
  override_dir: "E:/github/ai-home/flywheel-profile"  # 可选：私有 Home Repo 偏好与规则覆盖包

capabilities:
  skills_dirs:
    - "E:/github/ai-home/skills"                      # 可选：只读探测本地 SKILL.md

knowledge_sink:
  type: "local_markdown_wiki"                         # builtin_kb | local_markdown_wiki
  root: "E:/wiki"                                     # 挂载任意本地 Markdown / Obsidian 目录

runner:
  mode: "dry_run"                                     # dry_run | cli（配置本机 codex/claude CLI 复用订阅）
```

---

## 自动化测试

```bash
pytest -v
```

## License

[MIT](./LICENSE)

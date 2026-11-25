# Tasks: refactor-approval-channel-abstraction

- [x] 1. 新增 `backend/ai_flywheel/core/approval_channel.py`：`ApprovalChannel` 运行时协议与契约级 `ApprovalBusyError`；`orchestrator.py` 的 `ApprovalLockBusy` 继承该基类，构造参数与 `feishu_channel` 标注改为 `ApprovalChannel | None`【test】
- [x] 2. 把 `card` / `channel` / `check` / `listen` 四个实现迁入 `backend/ai_flywheel/adapters/channels/feishu/`，内部改为相对导入，通道只捕获 `ApprovalBusyError`【test】
- [x] 3. 把 `core/feishu_card.py`、`core/feishu_channel.py`、`core/feishu_check.py`、`core/feishu_listen.py` 改为薄再导出兼容层，旧导入与新导入解析到同一对象【test】
- [x] 4. `runtime.py` 从 `adapters.channels.feishu` 导入飞书类并标注 `self.feishu_channel: ApprovalChannel | None`；启动审计改读 `self.lark_runner`，启动 / 停止 / 点击路由 / 守护状态行为不变【test】
- [x] 5. 把 `app.py` 拆分为 `routes/overview.py`、`routes/governance.py`、`routes/runs.py`、`routes/automations.py` 四个 `APIRouter` 工厂；`app.py` 只保留 `create_app`、lifespan、MCP 挂载、CORS、SSE、路由挂载与 SPA 托管，且 ≤ 220 行（实测 168 行）【static】
- [x] 6. 新增 `tests/test_approval_channel_abstraction.py`：契约一致性、旧/新导入同源、忙碌错误继承、53 条应用路由逐条一致；同步更新 `tests/test_feishu_group1.py` 的死分支检查路径【test】
- [x] 7. 同步 `AGENTS.md` 包地图：`core/` 不再列出 `feishu_*` 实现，新增 `adapters/channels/` 与 `routes/`【static】
- [x] 8. 执行 `.venv/Scripts/python.exe -m pytest tests/test_approval_channel_abstraction.py tests/test_feishu_group1.py tests/test_feishu_group2.py tests/test_arch_invariants.py tests/test_control_plane_console.py tests/test_automation_console.py -q`（结果：106 passed）【test】
- [x] 9. 执行 `openspec validate --all --strict`（12 passed）与 `.venv/Scripts/python.exe scripts/verify_repo.py`（hygiene 261 files clean / openspec / pytest 398 passed，3/3 通过）【test】
- [x] 10. 以隔离 `--base-dir` 与独立端口真实启动 `flywheel serve`，请求 `/health`、`/llms.txt`、`/api/site/overview`、`/api/site/runs`、`/api/site/automations`、`/runs`、`/mcp` 与两个 404 分支后停止进程；应用日志显示 `Application shutdown complete`【real】

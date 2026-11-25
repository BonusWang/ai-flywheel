# Design: 修复飞书交互审批配置闭环、自检入口与文本降级表达

## 关键决策与取舍

### D1：纯文本审批通知的清洗、标点归一化与语义分级

- **问题**：`FeishuChannel._send_text_row()` 直接插值 `reasons[:2]`（未过 `sanitize()`），直接打印 `{w.target_kind}`，且无论退回原因一律输出「飞书按钮暂不可用，请到电脑前处理」。同时 `normalizers.py` 在为 Skill 更新追加说明时直接 `+= "；..."`，造成 `。；`。
- **方案**：
  1. 在 `adapters/runners/normalizers.py` 新增小工具 `_append_reason(base, suffix)`：去除 `base` 末尾的 `。；; ` 后再以 `；` 拼接，避免从源头制造 `。；`。
  2. 在 `core/feishu_channel.py` 新增 `_format_text_reasons(reasons)`：将每条原因先经 `sanitize(..., limit=180)`，再规范化其中的 `。；` 为 `；`、去除首尾多余 `。；; `，去重后以 `；` 连接。
  3. 列表项统一使用 `KIND_LABEL.get(w.target_kind, w.target_kind)`，与 `feishu_card.py` 的单一事实源一致。
  4. 新增 `_fallback_footer(avail, portal_url)`（按 `fcfg.interactive` 与退回原因/可用性分级）：
     - `not fcfg.interactive` → `当前未开启飞书交互卡片审批，请到电脑前处理（可在控制台「接入与设置」开启交互审批并完成自检）：{portal_url}`
     - `avail == "down:check_invalid"` → `飞书交互卡片尚未通过链路自检，请到电脑前处理（或在控制台完成链路自检）：{portal_url}`
     - 其他（监听不可用、发送降级、非 `serve` 进程） → `飞书按钮暂不可用，请到电脑前处理：{portal_url}`
- **备选与取舍**：曾考虑在 `feishu_text` 里彻底隐藏降级原因；但真实场景中「没开交互」和「链路故障」的处理动作完全不同，区分后既保留故障告警语义，又避免把正常默认配置误报成线上故障。

### D2：运行时热启飞书交互层与监听器即时唤醒

- **问题**：`FlywheelRuntime._build_feishu()` 仅在 `__init__` 调用一次；若启动时 `feishu_cli.enabled` 为 `False` 或 `user_id` 为空，通过控制台保存启用后 `rt.feishu_check` 仍为 `None`（接口 404）。此外 `FeishuClickListener._loop()` 在关闭态按 5 秒定长睡眠轮询 `should_run()`，用户打开开关后点自检会有最多 5 秒延迟。
- **方案**：
  1. `FeishuClickListener` 引入 `_wake = threading.Event()`，在 `wake()` 时置位；空闲等待改为等待 `_wake` 或 `_stop`（拆分为循环或共享信号），从而在 `update_runtime_config()` 切换 `interactive` 后立即启动或停止消费进程。
  2. `FlywheelRuntime.update_runtime_config()` 落盘并刷新 `self.cfg` 后：
     - 若 `self.feishu_channel is None` 且当前配置已满足 `_build_feishu()` 前提，尝试调用 `self._build_feishu()`（捕获 `LarkCliUnavailableError` 记告警不中断保存），若 `self.process_role == "serve"` 则调用 `self.start_feishu()`；
     - 若 `self.feishu_listener is not None`，调用 `self.feishu_listener.wake()`。
  3. `FeishuCheckService._run_inner()` 在检查 `enabled`/`user_id` 之后、等待监听器之前增加 `if not fcfg.interactive` 短路检查；`POST /api/site/feishu-check` 在 `not rt.cfg.delivery.feishu_cli.interactive` 时直接返回 HTTP 400。
  4. `GET /api/site/feishu-check` 统一补充返回 `enabled`、`interactive`、`interaction_ready`、`availability` 字段（向后兼容旧字段）。
- **备选与取舍**：曾考虑把 `delivery.feishu_cli.interactive` 列为 `STARTUP_ONLY_PATHS` 强制重启；但 `FeishuClickListener` 本身已按 `should_run` 设计为运行时响应，且 `openspec/specs/approval-card-channels/spec.md` 明确要求仅 `command`/`command_args`/`identity` 需重启生效，因此热唤醒与按需装配是更契合现行规格的最小改动。

### D3：前端「接入与设置」交互审批闭环与状态文案对齐

- **方案**：
  1. `frontend/src/types.ts` 与 `frontend/src/api.ts` 新增 `FeishuCheckStatus` 与 `api.startFeishuCheck(waitSeconds?)`。
  2. `ConnectPolicyPage.tsx` 在飞书 CLI 配置块内增加：
     - 「开启飞书交互审批卡片（直接在飞书内点击批准/拒绝）」复选框；
     - 「额外审批人 open_id（可选，逗号或换行分隔，默认包含接收人）」输入框；
     - 当前状态与自检区域：展示监听状态与自检结果，提供「保存并运行链路自检 / 运行链路自检」按钮，点击后自动调用 `POST /api/site/feishu-check` 并轮询状态直至通过/失败/超时，给出明确指引（请到飞书点击「我收到了」）。
  3. `WorkflowsPage.tsx` 根据 `st.enabled` 与 `st.interactive` 先判断「未启用」「仅文本通知（未开启交互卡片）」，再判断监听与自检状态。

## 真实验证方式

1. 自动化闸门：`python scripts/verify_repo.py --frontend`（卫生 → `openspec validate --all --strict` → `pytest` → 前端构建）全部通过。
2. 脱敏回归测试：覆盖 `reasons` 敏感片段清洗、`。；` 标点归一化、中文类型标签、三种退回文案分支、运行时热启飞书通道/监听器唤醒、`GET/POST /api/site/feishu-check` 状态与 400 短路。
3. 本机真实闭环验证（经维护者授权修改未入库的本机 `flywheel.yaml`）：
   - 启用 `delivery.feishu_cli.interactive: true`；
   - 以 `serve` 角色核验 `/health`、`data/FLYWHEEL-CORE.md`、`flywheel-recovery` 巡检及 `/api/site/feishu-check` 监听进入 `ready`；
   - 触发真实 `feishu-check` 确认测试卡片送达、改卡成功，并支持审批人点击完成全链路自检。

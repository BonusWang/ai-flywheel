# Tasks: fix-feishu-approval-loop-ux-and-config

- [x] 1. 修复 `adapters/runners/normalizers.py` 与 `core/feishu_channel.py`：消除审批原因 `。；` 拼接、对纯文本通知的 `reasons` 执行 `sanitize()` 脱敏、使用 `KIND_LABEL` 中文类型标签，并按未开启交互 / 未通过自检 / 链路暂不可用区分退回引导文案【test】
- [x] 2. 完善 `core/feishu_listen.py`、`core/feishu_check.py`、`runtime.py` 与 `app.py`：新增监听器 `wake()` 即时唤醒、支持配置热启飞书交互层、自检未开启交互模式时快速失败（400 / 立即 fail），并在 `GET /api/site/feishu-check` 返回 `enabled`/`interactive`/`interaction_ready`/`availability`【test】
- [x] 3. 更新前端 `types.ts`、`api.ts`、`ConnectPolicyPage.tsx`、`WorkflowsPage.tsx`：补齐飞书交互卡片开关、额外审批人配置、在线自检触发与轮询进度提示，并对齐审批链路状态文案，通过 `npm run build`【test】
- [x] 4. 在 `tests/test_feishu_group2.py`（及必要的接口测试）补充脱敏回归用例，并执行 `python scripts/verify_repo.py --frontend`、`openspec validate --all --strict` 与 `codegraph sync`【test】
- [x] 5. 按维护者授权在本机未入库的 `flywheel.yaml` 开启 `delivery.feishu_cli.interactive: true`，完成 `serve` 角色冒烟与真实飞书监听就绪及交互自检联调【real】

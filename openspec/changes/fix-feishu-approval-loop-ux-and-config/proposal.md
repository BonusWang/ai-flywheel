# Proposal: 修复飞书交互审批配置闭环、自检入口与文本降级表达

## Why

在真实运行批次进入人工审批阶段时，飞书通道投递出现如下现象与根因层级：

1. **直接现象（真实运行证据）**：某次批次进入 `AWAITING_APPROVAL` 后，`card_deliveries` 中 `feishu_cli` 记录为 `degraded`，原因为 `interactive unavailable (down:config)`，系统退回发送 `feishu_text` 纯文本消息，并在文末提示「飞书按钮暂不可用，请到电脑前处理」。同时纯文本通知中出现了 `。；` 标点叠加与裸英文枚举 `[flywheel_rule]` / `[skill_improvement]` / `[knowledge_doc]`。
2. **产品与配置闭环缺口**：
   - `delivery.feishu_cli.interactive` 默认关闭，但 Web 控制台「接入与设置」页仅暴露了 `enabled`、`user_id`、`notify_on_fyi`，缺少 `interactive` 开关、`approver_open_ids` 编辑和一键触发链路自检（`POST /api/site/feishu-check`）的入口，而「自动化任务」页却引导用户前往「接入与设置」开启交互模式与自检，导致用户无法在控制台完成闭环配置。
   - 若服务启动时 `feishu_cli` 尚未启用或未填 `user_id`，运行时通过 `/api/site/config` 补齐配置后，`FlywheelRuntime` 不会动态初始化飞书通道/监听器/自检服务，导致 `POST /api/site/feishu-check` 直接返回 404。
   - 当 `interactive` 关闭时调用自检，后端未做前置短路检查，而是空等 31 秒监听就绪后才报超时失败。
3. **纯文本降级消息的清洗与语义缺陷**：
   - `FeishuChannel._send_text_row()` 仅对写入项的 `title` 与 `summary` 调用了 `sanitize()`，**遗漏了对 `reasons`（审批触发原因）的清洗与脱敏**，违背 `approval-card-channels` 规格中“发往飞书的全部文本均须经清洗与脱敏”的契约。
   - `normalizers.py` 在为 Skill 更新追加说明时直接拼接 `"；..."`，若模型输出的 `approval_reason` 以句号结尾会产生 `。；`；`_send_text_row()` 拼接多个原因时同样未归一化尾标点，且条目类型直接暴露内部英文枚举 `target_kind` 而非中文标签 `KIND_LABEL`。
   - 降级文案将「主动未开启交互模式（`down:config`）」「已开启但未通过链路自检（`down:check_invalid`）」与「监听或发送故障」一律写成「飞书按钮暂不可用」，误导排查方向；前端 `WorkflowsPage` 也将未开启交互模式笼统显示为「监听已停止」。

## What Changes

- **外发纯文本清洗、格式与语义修正（`core/feishu_channel.py`、`adapters/runners/normalizers.py`）**：
  - 对纯文本审批通知中的 `reasons` 执行完整的 `sanitize()` 清洗与脱敏，并在归一化/拼接时消除 `。；` 叠加标点；
  - 纯文本条目清单统一复用 `feishu_card.KIND_LABEL`（笔记 / 规则 / Skill），避免暴露英文枚举；
  - 按实际退回原因区分纯文本尾注：未开启交互模式、未通过自检、或链路暂不可用，各自给出准确引导。
- **运行时热启飞书通道与自检前置校验（`runtime.py`、`core/feishu_listen.py`、`core/feishu_check.py`、`app.py`）**：
  - `FeishuClickListener` 新增 `wake()` 立即唤醒监听循环，切换 `interactive` 开关后无需苦等 5 秒轮询；
  - `FlywheelRuntime.update_runtime_config()` 在配置补齐飞书前提条件时支持动态装配（及 `serve` 角色下启动）飞书交互层，并在配置变更后唤醒监听器；
  - `FeishuCheckService._run_inner()` 在等待监听器前即检查 `fcfg.interactive`，未开启时立即给出明确失败原因；`POST /api/site/feishu-check` 在未开启交互模式时直接返回 400 提示；
  - `GET /api/site/feishu-check` 补充返回 `enabled`、`interactive`、`interaction_ready`、`availability` 字段，供前端准确区分「未开启交互」与「监听异常」。
- **Web 控制台飞书交互审批配置与自检闭环（`frontend/src/`）**：
  - `ConnectPolicyPage.tsx` 新增「开启飞书交互审批卡片」开关、「额外审批人 open_id」输入、实时链路状态展示及「运行飞书链路自检」按钮（含轮询进度与点击指引）；
  - `WorkflowsPage.tsx`、`api.ts`、`types.ts` 对齐扩展后的 `/api/site/feishu-check` 契约，准确区分未开启交互模式与监听异常。

## Capabilities

### New Capabilities
- 无（在现有 `approval-card-channels` 能力上修复与完善闭环）。

### Modified Capabilities
- `approval-card-channels`: 补齐纯文本审批通知的 `reasons` 清洗、中文类型标签与按退回原因区分的引导文案；明确交互模式与自检状态接口字段、自检前置短路及控制台配置与触发闭环。

## Impact

- 后端：`backend/ai_flywheel/core/feishu_channel.py`、`backend/ai_flywheel/core/feishu_check.py`、`backend/ai_flywheel/core/feishu_listen.py`、`backend/ai_flywheel/adapters/runners/normalizers.py`、`backend/ai_flywheel/runtime.py`、`backend/ai_flywheel/app.py`。
- 前端：`frontend/src/types.ts`、`frontend/src/api.ts`、`frontend/src/pages/ConnectPolicyPage.tsx`、`frontend/src/pages/WorkflowsPage.tsx`。
- 测试：`tests/test_feishu_group2.py`（脱敏回归，覆盖 `reasons` 清洗、中文类型标签、标点归一化、分场景退回文案、热启飞书交互层与自检状态接口）。
- 不改变 P00–P07 阶段副作用边界、安全阀优先权或默认关闭交互模式的安全默认契约。

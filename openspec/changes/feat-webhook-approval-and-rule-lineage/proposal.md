# Proposal: 通用 Webhook 审批通知与轻量规则沿革

## Why

两处缺口都不在新功能，而在**已经发生的事实没有被可靠地送出去、也没有被可靠地记下来**。

1. **除飞书之外没有第二个出站通道。** 当前 `delivery.webhook` 只有一个字段级的占位实现：配置项只有 `enabled` / `url`，投递时把运行编号、状态和整段 digest 直接 POST 出去，既不脱敏、不签名，也没有「等待审批」「运行失败」这两类真正需要人立刻知道的事件。于是：
   - 不使用飞书的部署没有任何渠道收到「批次挂起、等你审批」和「批次失败」；
   - 一旦把 webhook 指向外部服务，运行摘要、写入项标题里可能夹带的 `<private>` 片段与密钥样式字符串会原样离开本机；
   - 接收方无法验证请求确实来自本机飞轮，也无法在 HTTP 500 或超时时把这次通知与真实运行状态区分开——现有实现还会把异常抛到 P07，让一次网络抖动看起来像一次投递失败。
2. **规则只有状态，没有沿革。** `rules` 表记录 `status` / `version` / `revalidation_history`，但无法回答两个最基本的问题：这条规则**替代了哪一条**、它在**哪段时间内有效**。当一条规则被新版本取代时，旧规则仍然停在原来的状态里；休眠与唤醒也不留下生效窗口。结果是：控制台只能看到「现在有哪些规则」，看不到「这条规则从哪来、什么时候退场」。

公开仓库的约束让第 1 点尤其重要：外发通道必须默认不启用、默认脱敏、默认不打印密钥，签名密钥只能来自 gitignored 配置或环境变量。

## What Changes

- **`delivery.webhook` 配置升级**（`backend/ai_flywheel/config.py`）：
  - `WebhookDeliveryConfig` 新增 `secret`（HMAC-SHA256 签名密钥，可空）、`timeout_seconds`（默认 10）、`notify_on_fyi`（默认 false）；
  - `FLYWHEEL_WEBHOOK_URL` / `FLYWHEEL_WEBHOOK_SECRET` 环境变量覆盖，空值不覆盖已配置值；
  - `/api/site/connect` 与 `PUT /api/site/config` 的配置导出把 `delivery.webhook.secret` 置空并给出 `secret_configured`；系统设置留痕把该字段记为 `***`；保存配置时空值或 `[REDACTED]` 不会覆盖真实密钥。
- **通用 Webhook 通道**（新增 `backend/ai_flywheel/adapters/channels/webhook.py`）：
  - `WebhookChannel` / `send_webhook_notification(...)`，事件类型 `awaiting_approval` / `run_completed` / `run_failed` / `fyi`；
  - 全部外发字符串先过 `core/privacy.py` 的同一套门禁：剥离 `<private>` 片段、按 `SECRET_PATTERNS` 掩码；写入项压缩为 `action` / `target_kind` / `title` / `relative_path` / `sensitive`（`models.is_sensitive_write`）等字段；
  - 请求头 `Content-Type: application/json`、`User-Agent: ai-flywheel/<version>`，配置了密钥时附 `X-Flywheel-Signature: sha256=<hex>`（对原始 UTF-8 JSON body 计算）；
  - 网络错误、超时、非法 URL 与 HTTP 非 2xx 一律返回结构化结果 `{channel, sent, status_code, reason}`，绝不抛出；密钥不进入日志、返回值或运行记录。
- **投递编排接线**（`adapters/delivery.py` + P04 挂起 / 失败路径）：
  - `notify_approval_needed`、`notify_run_completed`、`notify_failure`、`notify_fyi` 四个入口；前三个与飞书互不依赖，`fyi` 受 `notify_on_fyi` 控制；
  - 运行挂起时在创建提问卡之后推送 `awaiting_approval`；P07 交付推送 `run_completed`；运行失败推送 `run_failed`（不再让 Webhook 异常影响 P07 终态）。
- **`flywheel doctor` 的 `channels` 检查**同时报告 Webhook：启用且 URL 非空为 PASS、启用但 URL 为空为 FAIL，签名只报 on/off。
- **规则沿革**（纯 SQLite，零图数据库）：
  - `rules` 表新增可空列 `supersedes_rule_id` / `effective_from` / `effective_to`，`migrations.py` 用幂等 `ALTER TABLE` 升级旧库，旧数据零丢失；
  - `RuleRecord` / `CreateRuleRequest` 增加对应字段；
  - 新建规则默认 `effective_from = now`；登记时声明 `supersedes_rule_id` 且被替代规则存在时，被替代规则转为 `retired`、写入 `effective_to`、追加 `revalidation_history` 的 `superseded` 事件与 `rule_superseded` 审计；
  - 状态流转到 `dormant` / `retired` 时关闭生效窗口，重新激活时刷新 `effective_from` 并清空 `effective_to`；自动休眠同样关闭窗口。
- **前端**：`types.ts` 的 `RuleRecord` 增加三个可选字段，规则卡片显示「替代 <id>」与「生效期 … ~ 现行/…」。
- **测试**：新增 `tests/test_webhook_and_rule_lineage.py`，用 `httpx.MockTransport` 验证签名、脱敏、`notify_on_fyi`、HTTP 500 / 超时与配置导出脱敏；验证旧库迁移、替代链与生效窗口。

## Capabilities

### New Capabilities

- `rule-lineage`：规则的轻量双时态沿革契约——替代指针、生效窗口、旧库幂等迁移、替代与状态流转对窗口的维护。

### Modified Capabilities

- `approval-card-channels`：新增「通用 Webhook 通知通道」一组增量要求（事件类型、载荷、脱敏、签名、失败处理、密钥掩码与 `doctor` 自检）；飞书通道的既有契约不变。

## Impact

- 代码新增：`backend/ai_flywheel/adapters/channels/webhook.py`、`tests/test_webhook_and_rule_lineage.py`。
- 代码修改：`config.py`（配置模型与环境变量）、`adapters/delivery.py`（四个通知入口与 P07 投递）、`core/pipeline/coordinator.py`（挂起时推送）、`core/pipeline/recovery.py`（失败时推送）、`core/doctor.py`（`channels` 检查）、`routes/overview.py`（配置导出脱敏）、`runtime.py`（保存配置时的密钥保留与留痕掩码）、`storage/db/schema.py`、`storage/db/migrations.py`、`storage/db/rule_repo.py`、`models.py`、`core/activation.py`。
- 前端：`frontend/src/types.ts`、`frontend/src/components/rules/RulesListTab.tsx`。
- 配置：`flywheel.yaml.example` 的 `delivery.webhook` 段补全新字段与说明。
- 兼容性：Webhook 默认关闭；未配置密钥时不带签名头；既有 `flywheel.yaml`（只有 `enabled` / `url`）继续可用；旧 SQLite 库启动时自动补齐三列，既有规则行原样保留。
- 行为变化：Webhook 的 HTTP 失败不再让 P07 失败——通道契约明确「通知失败只报告、不改变运行终态」；这是有意的修正，写入链路本身的失败语义不变。
- 不改变：P00–P07 阶段副作用边界、飞书通道的发送形态与重试、`is_sensitive_write` 判定、`resolve_runner_mode` 与安全阀语义。
- 不在范围：Webhook 投递重试与持久化外发箱（仍是一次性通知）、接收方回调、规则沿革的可视化时间轴。

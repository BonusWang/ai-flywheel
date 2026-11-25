# Tasks: 通用 Webhook 审批通知与轻量规则沿革

> 验收方式：`【test】` 自动测试 / `【static】` 代码与规格静态核对 / `【real】` 真实运行。
> 真实运行验证完成前，本变更不归档。

## 1. 配置与脱敏

- [x] 1.1 `config.py` 新增 `WebhookDeliveryConfig`（`enabled` / `url` / `secret` / `timeout_seconds` / `notify_on_fyi`）并挂到 `DeliveryConfig.webhook`；`load_config` 支持 `FLYWHEEL_WEBHOOK_URL` 与 `FLYWHEEL_WEBHOOK_SECRET`，空值不覆盖已有配置。【test】
- [x] 1.2 `/api/site/connect` 与 `PUT /api/site/config` 的配置导出把 `delivery.webhook.secret` 置空并给出 `secret_configured`；保存配置时空值或 `[REDACTED]` 不覆盖真实密钥；系统设置留痕把密钥记为 `***`。【test】
- [x] 1.3 `flywheel.yaml.example` 的 `delivery.webhook` 段补全字段与中文说明，并注明环境变量覆盖。【static】

## 2. Webhook 通道

- [x] 2.1 新增 `adapters/channels/webhook.py`：`WebhookChannel` / `send_webhook_notification`，事件类型 `awaiting_approval` / `run_completed` / `run_failed` / `fyi`，载荷含 `event_type` / `run_id` / `status` / `summary` / `portal_url` / `reasons` / `planned_writes` / `sent_at`。【test】
- [x] 2.2 外发字符串统一经 `core/privacy.py` 剥离 `<private>` 并按 `SECRET_PATTERNS` 掩码；写入项按 `is_sensitive_write` 标记 `sensitive`。【test】
- [x] 2.3 配置密钥时计算 `X-Flywheel-Signature: sha256=<hex>`（对原始 UTF-8 JSON body），并带 `Content-Type` 与 `User-Agent: ai-flywheel/<version>`。【test】
- [x] 2.4 连接失败 / 超时 / 非法 URL / HTTP 非 2xx 返回 `{channel, sent, status_code, reason}`，绝不抛出；密钥不出现在结果中。【test】
- [x] 2.5 `notify_on_fyi` 只控制 `fyi`，审批、完成与失败通知不受影响。【test】

## 3. 投递编排与自检

- [x] 3.1 `DeliveryAdapter` 新增 `notify_approval_needed` / `notify_run_completed` / `notify_failure` / `notify_fyi`；P07 的 `deliver` 改为经通道发送并把结果写进交付摘要。【test】
- [x] 3.2 运行挂起时在提问卡创建后推送 `awaiting_approval`；运行失败时在 `_alert` 中推送 `run_failed`，且飞书关闭时 Webhook 仍生效。【static】
- [x] 3.3 `flywheel doctor` 的 `channels` 检查报告 Webhook：启用 + URL 非空为 PASS，启用 + URL 为空为 FAIL，只报签名 on/off。【test】

## 4. 规则沿革

- [x] 4.1 `schema.py` 的 `rules` 表新增 `supersedes_rule_id` / `effective_from` / `effective_to`；`migrations.py` 新增幂等 `_migrate_rule_lineage`，旧库只做 `ALTER TABLE ADD COLUMN`。【test】
- [x] 4.2 `models.py` 的 `RuleRecord` 增加三个可空字段，`CreateRuleRequest` 增加 `supersedes_rule_id`。【static】
- [x] 4.3 `rule_repo.py` 的 `upsert_rule` / `_row_to_rule` 持久化并读取沿革字段；`mark_dormant_stale_rules` 在自动休眠时写入 `effective_to`。【test】
- [x] 4.4 `activation.py` 的 `register_rule`：新规则默认 `effective_from`；声明替代时停用旧规则、写 `effective_to`、追加 `superseded` 历史与 `rule_superseded` 审计；重复登记不重复留痕。【test】
- [x] 4.5 `transition_rule_status`：转入 `dormant` / `retired` 写 `effective_to`；重新激活清空 `effective_to` 并补齐 / 刷新 `effective_from`。【test】
- [x] 4.6 前端 `types.ts` 增加三个可选字段，`RulesListTab.tsx` 渲染「替代 <id>」与「生效期 … ~ 现行/…」。【test】

## 5. 验收

- [x] 5.1 `tests/test_webhook_and_rule_lineage.py` 覆盖 Webhook 签名 / 脱敏 / `notify_on_fyi` / HTTP 500 / 超时 / 配置导出脱敏，以及旧库迁移 / 替代链 / 生效窗口。【test】
- [x] 5.2 `.venv/Scripts/python.exe -m pytest tests/test_webhook_and_rule_lineage.py tests/test_onboarding_cli.py tests/test_arch_invariants.py -q` 通过。【test】
- [x] 5.3 `openspec validate --all --strict` 通过。【test】
- [x] 5.4 `.venv/Scripts/python.exe scripts/verify_repo.py --frontend` 四项全绿（hygiene / openspec / pytest / frontend）。【test】
- [x] 5.5 真实运行：`serve` 中把 webhook 指向本地接收端，触发一次真实挂起，核对事件、签名、脱敏与「接收方 500 不影响运行」（已通过本地 HTTPServer 真实 POST 实测：`awaiting_approval` 200 OK、HMAC 验签通过、`<private>` 剥离含文件名 slug 修复、500 非致命批次 PASS）。【real】

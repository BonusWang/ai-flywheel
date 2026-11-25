# approval-card-channels —— 通用 Webhook 通知通道

> 2026-11-25。本变更只新增一个与飞书并列的通用出站通知通道，飞书通道的发送形态、重试、回写与安全默认全部不变。

## ADDED Requirements

### Requirement: 通用 Webhook 通知通道
`delivery.webhook` 启用且 `url` 非空时，系统 SHALL 以 JSON POST 向该地址推送四类事件：`awaiting_approval`（运行挂起等待审批）、`run_completed`（运行完成）、`run_failed`（运行失败）、`fyi`（日常通知）。载荷 MUST 包含 `event_type`、`run_id`、`status`、`summary`、`portal_url`、`reasons`、`planned_writes` 与 `sent_at`；每个写入项 MUST 只带 `action`、`target_kind`、`title`、`relative_path`、`sensitive` 这些紧凑字段，`sensitive` MUST 与 `models.is_sensitive_write` 的判定一致。`fyi` MUST 受 `notify_on_fyi` 控制，默认关闭；审批、完成与失败通知 MUST NOT 受该开关影响。Webhook 未启用或 `url` 为空时 MUST 不发起任何请求，并返回说明原因的结构化结果。

#### Scenario: 运行挂起时推送审批通知
- **WHEN** Webhook 已启用并配置了 URL，某次运行挂起等待审批
- **THEN** 该地址收到一条 `event_type = awaiting_approval` 的 JSON 请求，载荷列出挂起原因与本次的写入项

#### Scenario: 运行失败时推送告警
- **WHEN** Webhook 已启用并配置了 URL，某次运行在某个节点失败
- **THEN** 该地址收到一条 `event_type = run_failed` 的 JSON 请求，载荷带失败原因

#### Scenario: 日常通知默认不推送
- **WHEN** Webhook 已启用而 `notify_on_fyi` 关闭，系统产生一条 `fyi` 通知
- **THEN** 不发起请求，结果说明 `notify_on_fyi` 未开启

#### Scenario: 启用但未配置 URL
- **WHEN** Webhook 已启用但 `url` 为空
- **THEN** 不发起请求，结果说明 URL 为空

### Requirement: Webhook 外发内容脱敏
Webhook 载荷中的全部字符串（摘要、标题、原因、写入项标题与相对路径、审批理由等）MUST 在序列化之前经过 `core/privacy.py` 的同一套门禁：`<private>...</private>` 片段 MUST 被剥离，`SECRET_PATTERNS` 命中的密钥样式内容 MUST 被掩码。通道 MUST NOT 自建第二套密钥规则。脱敏不得抛出：一条通知的问题 MUST NOT 让运行失败。

#### Scenario: 摘要含 private 片段
- **WHEN** 待推送的摘要里有一段 `<private>...</private>`
- **THEN** 请求体中不出现这段内容

#### Scenario: 写入项标题含密钥样式字符串
- **WHEN** 某个写入项的标题或审批理由里含 API key 样式的字符串
- **THEN** 请求体中该字符串被掩码，其余字段照常送达

### Requirement: Webhook 签名与失败处理
配置了 `secret` 时，每个请求 MUST 携带 `X-Flywheel-Signature: sha256=<hex>`，其中 `<hex>` 是对**原始 UTF-8 JSON body** 计算的 HMAC-SHA256。每个请求 MUST 携带 `Content-Type: application/json` 与 `User-Agent: ai-flywheel/<version>`。连接失败、超时、非法 URL 与 HTTP 非 2xx MUST NOT 抛出异常，MUST 返回 `{channel, sent, status_code, reason}` 结构；这类失败 MUST NOT 改变运行状态与终态。`secret` MUST NOT 出现在日志、返回值、运行记录或配置导出中；配置导出 MUST 把该字段置空并给出 `secret_configured` 布尔值。

#### Scenario: 签名可被验证
- **WHEN** Webhook 配置了签名密钥并发送一条通知
- **THEN** 请求头中的签名等于用同一密钥对请求体计算的 `sha256=<hex>`

#### Scenario: 接收方返回 500
- **WHEN** Webhook 接收方返回 HTTP 500
- **THEN** 调用方得到 `sent = false` 与状态码 500，不抛出异常，运行状态不变

#### Scenario: 请求超时
- **WHEN** Webhook 请求超时
- **THEN** 调用方得到 `sent = false` 与原因说明，不抛出异常，运行状态不变

#### Scenario: 配置导出不泄露密钥
- **WHEN** 已配置 Webhook 签名密钥，维护者打开或导出站点配置
- **THEN** 导出中的 `delivery.webhook.secret` 为空，且 `secret_configured` 为真

### Requirement: 通道自检覆盖 Webhook
`flywheel doctor` 的 `channels` 检查 MUST 在 Webhook 启用时报告其可用性：启用且 URL 非空为 `PASS`，启用但 URL 为空为 `FAIL`。检查结果 MUST 只报告签名是否开启，MUST NOT 输出密钥内容。

#### Scenario: 启用但未填 URL 的自检
- **WHEN** `delivery.webhook.enabled` 为真而 `url` 为空，运行 `flywheel doctor`
- **THEN** `channels` 检查为 `FAIL` 并指明 URL 为空

#### Scenario: 签名开启的自检
- **WHEN** Webhook 启用、URL 非空且配置了签名密钥
- **THEN** `channels` 检查为 `PASS`，说明中只写签名已开启

# Design: 通用 Webhook 审批通知与轻量规则沿革

## 1. 为什么是「通道 + 适配器」而不是继续扩展 `DeliveryAdapter._post_webhook`

**现状**：`DeliveryAdapter` 里有一个 `_post_webhook`，只发 `run_id / status / phase / digest`，不脱敏、不签名，HTTP 失败直接 `raise_for_status()` 抛到 P07，P07 因此失败。

**取舍**：

| 方案 | 结论 |
|---|---|
| 继续把逻辑写在 `delivery.py` | 拒绝：投递编排与 HTTP 细节混在一起，无法单测传输，也无法复用脱敏规则 |
| 新建 `adapters/channels/webhook.py` | **采用**：与 `adapters/channels/feishu/` 同级，符合 AGENTS.md 的分层；`DeliveryAdapter` 只做事件编排 |
| 复用飞书卡片渲染 | 拒绝：Webhook 接收方要的是结构化 JSON，不是飞书卡片 DSL |

**结果**：`WebhookChannel` / `send_webhook_notification` 负责「一条 JSON 通知怎么发」，`DeliveryAdapter` 负责「什么事件该发」。`DeliveryAdapter` 每次通知都从当前 `cfg.delivery.webhook` 构造通道（除非测试注入了带 `MockTransport` 的通道），因为保存设置会整体替换 `cfg.delivery`，缓存通道会拿到旧配置。

## 2. 为什么失败只报告、不抛出

通知是**旁路**：运行记录与提问卡才是事实来源。把网络抖动升级为运行失败，会让「写完了但 webhook 挂了」看起来像「批次失败」，与 P07 的终态契约冲突。因此：

- `send_webhook_notification` 捕获传输层与 URL 解析异常，返回 `{channel, sent, status_code, reason}`；
- `DeliveryAdapter.deliver` 把结果格式化为 `PASS (200)` / `FAILED (HTTP 500)` 写进 P07 的节点载荷，运行仍是 `PASS`；
- 例外：签名密钥缺失或配置错误属于用户配置问题，仍以 `FAILED (<reason>)` 明示，不静默。

**这是对本变更前行为的修正**：旧实现让 P07 因 webhook 失败而失败。变更在 proposal 的 Impact 里明写。

## 3. 脱敏：只允许一套规则

`core/privacy.py` 的 `SECRET_PATTERNS` 是 AGENTS.md 指定的唯一密钥形状来源。Webhook 的 `sanitize_outbound` 组合两个既有函数：

1. `sanitize_and_check_secrets(text, refuse_secrets=False)`：剥离 `<private>...</private>`；
2. `redact_secrets(...)`：按 `SECRET_PATTERNS` 掩码。

`refuse_secrets=False` 是关键：写入链路遇到凭据样式会**拒绝**，通知链路不能因为一句话就拒绝发送，只能掩码。任何字段在进入 `json.dumps` 之前都经过这一步；`portal_url` 与本机构造的字段也一并处理，避免以后有人把外部输入塞进来。

## 4. 签名格式

`X-Flywheel-Signature: sha256=<hex>`，HMAC-SHA256 over **实际发送的原始字节**（`json.dumps(payload, ensure_ascii=False).encode("utf-8")`）。

- 不使用时间戳/防重放头：接收方可以自行按 `sent_at` 做窗口校验，签名保持最小可用；
- 不用 `json=` 让 httpx 重新序列化：签名必须对应字节本身，而不是「大概相同的 JSON」；
- `User-Agent: ai-flywheel/<version>` 直接读 `ai_flywheel.__version__`，满足「版本只有一个来源」；
- 密钥只参与 HMAC，不进返回值、不进日志、不进 `doctor` 输出、不进配置导出。

## 5. `notify_on_fyi` 只控制 `fyi`

飞书的 `notify_on_fyi` 控制「完成后的一条摘要」。Webhook 的四类事件里，审批与失败是**必须送达**的，`run_completed` 是运行终态的可观测性，只有 `fyi` 是纯日常通知。因此：

- `awaiting_approval` / `run_completed` / `run_failed`：启用即发送；
- `fyi`：`notify_on_fyi = true` 才发送；
- 当前流水线在 P07 只发 `run_completed`，没有额外的日常通知调用；`notify_fyi` 作为通道的公开入口保留给日常通知，开关语义已由测试钉住，避免以后新增调用时绕过门禁。
- 这样既不改变旧配置（enabled + url 即收到完成通知），又给日常通知一个明确的开关。

## 6. 接线位置

| 事件 | 位置 | 理由 |
|---|---|---|
| `awaiting_approval` | `core/pipeline/coordinator.py`，创建提问卡与门户投递记录之后 | 此刻原因、写入项、门户地址都已就绪；与飞书块并列，互不依赖 |
| `run_completed` | `adapters/delivery.py::deliver`（P07） | 唯一的终态交付点，已带 `written_paths` 与 digest |
| `run_failed` | `core/pipeline/recovery.py::_fail_run` 的 `_alert` 回调 | 与飞书告警同一处，锁与终态已处理完 |

`_alert` 的守卫从「applied 且飞书启用」改为「applied」，再分别判断两个通道；这保证飞书关闭时 Webhook 仍能收到失败告警。

## 7. 规则沿革：为什么是三列，不是一张表

**备选**：

| 方案 | 结论 |
|---|---|
| 新建 `rule_lineage` 关系表 | 拒绝：一个 `supersedes` 指针 + 两个时间戳就是全部信息，关系表只增加 join |
| 图数据库 | 拒绝：与「本地优先、零外部依赖」冲突，收益为零 |
| `rules` 表加三个可空列 | **采用**：迁移是纯 `ALTER TABLE ADD COLUMN`，旧库零重建、零丢失；查询就是普通 `SELECT` |

**语义**：

- `supersedes_rule_id`：新规则指向被它替代的旧规则，**只指向一条**。一条旧规则被多条新规则声明替代时保留最后写入的指针（`COALESCE` 保留已有值，首次写入生效）。
- `effective_from` / `effective_to`：闭开区间的直觉写法——`[effective_from, effective_to)`，`effective_to` 为空表示「现行」。
- 失败与例外：被替代规则已经是 `retired` 时不再重复写历史/审计；指向不存在的规则只保存指针，不阻断登记（登记是主操作，沿革是附加信息）。

**upsert 的取舍**：`effective_from = COALESCE(excluded.effective_from, rules.effective_from)` 防止旧调用方把手工写入的起点抹成空；`effective_to = excluded.effective_to` 有意采用「当前记录说了算」，否则重新激活时无法把终点清空。所有会改 `effective_to` 的路径都先 `get_rule` 再改再 upsert，因此不会误清。

## 8. 真实验证方式

- **test 级（本变更提供）**：`tests/test_webhook_and_rule_lineage.py` 用 `httpx.MockTransport` 验证签名、脱敏、`notify_on_fyi`、HTTP 500 / 超时、配置导出脱敏；用真实 SQLite 文件验证旧库迁移、替代链、生效窗口。
- **real 级（归档前由维护者补）**：在本机 `flywheel serve` 运行中，把 `delivery.webhook.url` 指向一个本地接收端（例如 `python -m http.server` 之外的 echo 服务），触发一次真实批次挂起，核对：
  1. 接收端收到 `awaiting_approval`，签名字段可用同一密钥复算通过；
  2. 摘要与写入项中没有 `<private>` 片段或密钥样式字符串；
  3. 接收端返回 500 时，运行仍停在 `AWAITING_APPROVAL`，P07 不因通知失败而失败；
  4. 控制台规则页出现「替代 …」「生效期 … ~ 现行」。
- 未完成 real 验证前，本变更不归档（AGENTS.md：归档是人的决定）。

## 9. 风险与边界

- Webhook 不做重试与持久化外发箱：一次失败就是一次失败，`reason` 留在 P07 节点载荷里。若未来需要「必达」，应复用 `storage/outbox.py`，而不是在这里加重试循环。
- `run_completed` 的 `summary` 携带整段 digest，体积可能较大；接收方需自行限制。飞书路径的 3500 字截断不适用于 Webhook，因为 JSON 接收方通常按字段处理。
- 沿革字段不参与 `match_rules` 过滤：规则是否能被命中仍由 `status` 决定，`retired` 规则本来就不参与匹配。

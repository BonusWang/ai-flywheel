# control-plane-console Specification Delta

## ADDED Requirements

### Requirement: 知识保鲜台账同步 Wiki 归档状态并尊重人工/自动归档边界
系统 SHALL 在 `sync_fts_index` 扫描挂载知识库时同步文档 frontmatter 中的 `status` 与 `last_verified` 到 `knowledge_card_metrics` 台账：
- 当源文档 frontmatter 的 `status` 为 `archived` 或 `superseded` 时，系统 MUST 将对应台账记录的 `freshness_state` 同步为相同终态，使其自动退出「待复核」清单；
- 当某篇文档在飞轮台账中已处于 `archived` 或 `superseded` 时，常规 FTS 同步 MUST 保持其归档状态不变，仅当源文档 frontmatter 出现严格更新的 `last_verified` 日期（且 `status` 非归档态）时才自动重新激活为 `fresh`。

#### Scenario: Wiki 源文件标记 status archived 后同步退出待复核
- **WHEN** 某篇过期笔记的 Markdown frontmatter 被更新为 `status: archived` 并触发 `sync_fts_index()`
- **THEN** `knowledge_card_metrics` 中该文档的 `freshness_state` 必须更新为 `archived`
- **AND** `GET /api/site/knowledge` 中该文档的 `is_expired` 必须为 `false`，不再计入 `expired_total`

#### Scenario: 已在飞轮台账归档的笔记在源文件未更新时保持归档，源文件更新后自动重新激活
- **WHEN** 某篇笔记在飞轮台账被标为 `archived`，其源文件 `last_verified` 未变化时执行 `sync_fts_index()`
- **THEN** 该笔记在台账中必须保持 `archived` 不被覆盖回 `fresh`
- **AND** 当源文件 `last_verified` 更新为更晚日期后再次同步，该笔记必须重新激活为 `fresh` 并重算 `valid_until`

### Requirement: 周五蒸馏 Tier 1 自动退场超期零助益知识笔记（仅改台账）
`core/dream.py` 的周五蒸馏 Tier 1 SHALL 自动将超过 `valid_until` 满 `stale_card_grace_days`（默认 30 天）且 `helped_count == 0` 的知识卡片在飞轮台账 `knowledge_card_metrics` 中标记为 `archived` 并写入审计事件，同时绝不修改或删除知识库磁盘文件；只读预览 `preview_dream_consolidation` MUST 透出 `auto_archived_cards` 候选清单且零写入。

#### Scenario: 超期满 30 天且零助益的卡片在蒸馏时自动退场，有助益卡片保留待复核
- **WHEN** 存在卡片 A（`valid_until` 早于今天 40 天且 `helped_count = 0`）与卡片 B（`valid_until` 早于今天 40 天但 `helped_count = 2`），先调用 `preview_dream_consolidation` 再调用 `run_dream_consolidation`
- **THEN** 只读预览必须在 `auto_archived_cards` 中列出卡片 A、不包含卡片 B，且不修改数据库
- **AND** 执行蒸馏后，卡片 A 的台账 `freshness_state` 变为 `archived` 并写入审计事件，卡片 B 保持 `needs_recheck`，且两篇磁盘源文件字节内容零变化

### Requirement: 控制台知识保鲜纯台账兜底复核接口与弹窗交互
系统 SHALL 提供 `POST /api/site/knowledge/freshness` 接口与阅读弹窗操作栏，支持维护者在控制台对知识笔记保鲜台账执行兜底操作（`verify` 顺延有效、`archive` 移出待复核、`restore` 恢复跟踪、`set_tier` 调整 30/90/180 天档位），所有操作 MUST 仅更新 SQLite 台账与审计事件并刷新核心索引卡，绝不修改知识库源文件。

#### Scenario: 维护者在控制台弹窗确认有效或归档仅更新台账
- **WHEN** 客户端调用 `POST /api/site/knowledge/freshness` 对一篇过期卡片执行 `action="verify"` 或 `action="archive"`
- **THEN** 接口返回 HTTP 200 及更新后的新鲜度记录与 `expired_total`，对应审计事件入库；`verify` 刷新 `last_verified_at`/`valid_until` 并置 `fresh`，`archive` 置 `archived` 退出待复核
- **AND** 挂载知识库中对应的 `.md` 源文件保持零写入

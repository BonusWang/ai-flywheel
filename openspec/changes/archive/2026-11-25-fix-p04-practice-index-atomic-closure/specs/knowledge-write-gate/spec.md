## ADDED Requirements

### Requirement: 项目实战卡与项目笔记索引原子登记

当 Schema 治理的项目实战卡 `project-practice-note` 或 `project-practice-lite` 写入成功时，系统 SHALL 在同一受控事务中确保对应项目的 `实战笔记/笔记索引.md` 含该卡片的精确链接条目。
主卡或索引任一步失败时 MUST 恢复两者写前字节，且不得把只完成一半的状态报告为成功。

索引条目 MUST 包含卡片日期、卡片类型和完整 Wiki 相对链接。系统 MUST 在写入前重新读取索引当前内容；
同一张卡片已有指向其路径的链接时 MUST 视为已登记，不新增重复条目。不同卡片并发登记同一索引时 MUST
串行完成读改写并保留所有不同条目。

项目索引缺失、索引类型不是 `project-practice-index`、路径逃出知识库根或索引写后校验失败时，
系统 MUST 抛出 `KnowledgeWriteError` / `WikiGateError`，且主卡不得落盘。

D2 写后钩子的 `--write-set` MUST 同时包含主卡与项目索引。成功、失败和回滚证据 MUST 记录索引目标、
新增条目、写前后 SHA-256、最终状态及回滚状态。

#### Scenario: 重卡原子登记

- **WHEN** 重卡与其项目笔记索引均通过写前校验且索引还没有该卡链接
- **THEN** 主卡写入，索引增加且只增加一条精确链接，回执状态为 `update`

#### Scenario: 轻记卡原子登记

- **WHEN** `project-practice-lite` 写入带有项目笔记索引的项目
- **THEN** 索引条目类型标为轻记卡，并与主卡在同一事务中通过或回滚

#### Scenario: 重复执行不重复登记

- **WHEN** 同一卡片内容再次执行且索引已有指向其路径的链接
- **THEN** 索引字节不变，回执状态为 `already_present`

#### Scenario: 并发共享索引

- **WHEN** 两张不同卡片同时写入同一项目
- **THEN** 索引锁使两次读改写串行，最终索引同时包含两条链接且各仅一次

#### Scenario: 索引在审批后消失

- **WHEN** P03 后项目索引被删除或移走，P04 尝试写入该卡
- **THEN** P04 失败关闭，主卡不在磁盘上，会话恢复为 `pending`

#### Scenario: 后检失败双路径回滚

- **WHEN** 主卡与索引写入后 D2 返回阻断
- **THEN** 主卡恢复为缺席、索引恢复 preimage，证据记录两处回滚，P04 为 `FAILED`

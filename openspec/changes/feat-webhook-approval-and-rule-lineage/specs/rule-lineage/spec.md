# rule-lineage —— 规则的轻量双时态沿革

> 2026-11-25。纯 SQLite 实现，不引入图数据库：沿革是两张可空列与两条时间戳，不是一张新的关系网。

## ADDED Requirements

### Requirement: 规则沿革字段与幂等迁移
`rules` 表 MUST 提供三个可空列：`supersedes_rule_id`（本规则替代的规则编号）、`effective_from`（生效起点）、`effective_to`（生效终点，空表示仍在生效）。数据库初始化时 MUST 以幂等方式为既有库补齐这三列（`ALTER TABLE ... ADD COLUMN`），MUST NOT 重建表、MUST NOT 丢失既有规则行；补齐前的行，其沿革字段保持为空。

#### Scenario: 旧库升级
- **WHEN** 一个不含这三个列的既有 SQLite 库首次被当前版本打开
- **THEN** 三列被补齐，既有规则行与其原有字段值全部保留

#### Scenario: 重复启动不重复迁移
- **WHEN** 已补齐三列的库再次启动
- **THEN** 迁移不报错，也不改变任何既有数据

### Requirement: 新规则的默认生效起点
新建规则时系统 MUST 写入 `effective_from = 当前时间`、`effective_to = 空`。登记请求声明的 `supersedes_rule_id` MUST 原样保存在新规则上；该编号不存在时 MUST 只保存指针，MUST NOT 因此让登记失败。

#### Scenario: 新建规则
- **WHEN** 维护者新建一条规则
- **THEN** 该规则的 `effective_from` 已写入，`effective_to` 为空

#### Scenario: 指向不存在的规则
- **WHEN** 登记请求声明的 `supersedes_rule_id` 在系统里不存在
- **THEN** 新规则照常建立并保存该指针，没有规则被停用

### Requirement: 替代即停用
登记请求声明的 `supersedes_rule_id` 指向一条已存在的规则，且该规则尚未 `retired` 时，系统 MUST 把它转为 `retired`、写入 `effective_to = 当前时间`、追加一条 `revalidation_history` 记录（`event = superseded`，含 `superseded_by`），并追加 `rule_superseded` 审计事件。该规则已经是 `retired` 时 MUST 保持不变，MUST NOT 重复追加历史或审计。

#### Scenario: 替代一条现行规则
- **WHEN** 新规则的登记声明替代某条 `unverified_active` 规则
- **THEN** 新规则保存替代指针与生效起点，旧规则变为 `retired`、`effective_to` 已写入，历史中含 `superseded` 事件，审计中含一条 `rule_superseded`

#### Scenario: 重复登记不重复留痕
- **WHEN** 再次登记一条规则，其声明的被替代规则已经是 `retired`
- **THEN** 该规则的状态、生效终点与历史不再变化，审计不新增重复事件

### Requirement: 生效窗口随状态流转
规则转入 `dormant` 或 `retired` 时，系统 MUST 写入 `effective_to = 当前时间`（自动休眠同样适用）。规则重新激活为 `unverified_active` 或 `verified_active` 时，系统 MUST 清空 `effective_to`，并 MUST 保证 `effective_from` 有值：缺失时补齐，从 `dormant` / `retired` 回来时刷新为当前时间。

#### Scenario: 休眠关闭窗口
- **WHEN** 维护者把一条规则转为 `dormant`，或规则因长期零命中被自动休眠
- **THEN** 该规则的 `effective_to` 已写入

#### Scenario: 唤醒重新开窗
- **WHEN** 维护者把一条 `dormant` 或 `retired` 的规则重新激活
- **THEN** 该规则的 `effective_to` 为空，`effective_from` 刷新为本次激活时间

#### Scenario: 停用关闭窗口
- **WHEN** 维护者把一条规则转为 `retired`
- **THEN** 该规则的 `effective_to` 已写入

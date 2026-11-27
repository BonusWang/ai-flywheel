# write-plan-contract Specification Delta

## ADDED Requirements

### Requirement: 领域渐进完善规则与既有领域资产进入 P03 规划
当挂载 Wiki 在 `.wiki-schema.md` 中声明了 `content_contract.deposit_routing` 时，P03 提示词输入 SHALL 一并携带该契约的领域路由规则与每个候选项目的既有领域页清单（`domain_inventory`）。

提示词 MUST 明确项目知识「渐进完善」规划规则：
- 保留至少一个主经验卡名额后，在单批写入上限内为 P02 命中的 `domain_signals` 规划对应项目领域的 `knowledge_doc` 写入项；
- 命中数据库表线索时 MUST 遵循「一表一页、只记实战已证字段、不要求全表 DDL」：若目标表名已出现在候选项目的 `domain_inventory` 中，动作 MUST 为 `update`（增量回填新查证的字段、枚举、关联键或坑）；若尚未建页，动作为 `create`；
- 命中项目配置线索时，不得以「无配置变更」为由跳过查实且需跨会话记忆的配置事实；若既有配置页已存在则优先 `action="update"` 增量补充，且 MUST 严守禁止输出密码或密钥明文的约束；
- 挂载 Wiki 未声明 `deposit_routing` 时，P03 提示词与输出契约 MUST 保持既有行为不变。

#### Scenario: 挂载 Wiki 声明四域路由时进入 P03 输入
- **WHEN** 挂载 Wiki 的 `.wiki-schema.md` 声明了 `content_contract.deposit_routing`，且候选项目 alpha 的数据库结构目录已存在表 `orders.md`
- **THEN** P03 提示词的输入 JSON 包含 `deposit_routing` 规则及 alpha 的 `domain_inventory`（含 `orders`），并明确已有表增量 `update`、缺页 `create` 与配置页禁落凭据明文的规划约束

#### Scenario: 挂载 Wiki 未声明四域路由时保持兼容
- **WHEN** 挂载 Wiki 的 `.wiki-schema.md` 未声明 `content_contract.deposit_routing`
- **THEN** P03 提示词不包含 `deposit_routing` 与 `domain_inventory`，规划行为与既有契约完全一致

# session-review-contract Specification Delta

## ADDED Requirements

### Requirement: 审查结果抽取项目领域核实线索
P02 提示词与结果归一化器 SHALL 在 `findings` 项中支持可选字段 `domain_signals`，用于结构化记录会话中真实查证或核实过的项目领域知识线索：

- `database_tables`：会话中实际查询、读码或核实过的业务表及其已证字段、枚举值、关联键与边界坑（不要求完整全表 DDL）；
- `project_configs`：会话中查实、核对且具有跨会话复用价值的项目配置事实（如数据源/实例与逻辑库映射、环境连库约束等），严禁包含密码、Token 或私钥明文；
- `architecture_flows`：会话中核实的服务协作拓扑或交互链路；
- `business_calibers`：会话中拍板或核实的业务算法与统计口径。

提示词 MUST 明确要求只记录会话中真实核实过的线索，未涉及的领域留空，不得编造或泛化推测；模型省略 `domain_signals` 时，归一化器 MUST 视为空对象且不得因此判定审查失败。

#### Scenario: 排查会话提取表结构与配置线索
- **WHEN** 送审会话包含对某业务表关键字段与枚举的查证记录以及某数据源连库约束的核实记录
- **THEN** P02 审查结果中对应 finding 的 `domain_signals.database_tables` 包含该表与已证字段，`domain_signals.project_configs` 包含该配置事实且不含密码明文

#### Scenario: 无领域线索的普通会话兼容缺省
- **WHEN** 模型返回的 finding 未包含 `domain_signals` 字段
- **THEN** 归一化器将其补齐为空结构，不触发审查失败或丢弃该 finding

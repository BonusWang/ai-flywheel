# Change: 项目领域知识渐进式沉淀与四域路由感知

## Why

当前 P02–P04 沉淀链路在挂载 Schema 治理的 Wiki 时，存在三处知识累积断点，导致项目排查与需求开发会话处理得越多，项目分区中的基础资产（表结构、项目配置、架构与算法页）却没有随之同步完善：

1. **未消费 Schema 的 `deposit_routing` 契约**：`adapters/wiki/schema.py` 与 `adapters/wiki/router.py` 目前只读取 `page_types` 与 `partitions`，且仅路由 `t5_adapter` 已登记的经验卡类型（`project-practice-note` / `project-practice-lite`），忽略了 `.wiki-schema.md` 中声明的 `content_contract.deposit_routing`（数据库结构 / 系统架构 / 业务算法 / 项目配置四域路由）。
2. **P02 未结构化抽取项目领域核实事实**：`adapters/runners/prompts.py` 的 P02 审查提示词只提取通用 `facts` / `anomalies`，未区分会话中是否实际查证过业务表字段/枚举/关联键/坑，或查实并需要跨会话记住的项目配置事实（如数据源/实例与库名映射、连库约束等），导致 P03 缺乏领域分流依据。
3. **P03 与门禁缺少「渐进完善」与增量回填机制**：项目实践中，排查与开发极少做全表 DDL 盘点，也未必伴随配置变更，往往只核实了某张表的若干关键字段/枚举或某条需记忆的环境配置事实。当前链路既未向 P03 提供目标项目已有的表结构/配置页索引摘要，也未支持按「一表一页、只记实战已证字段、已有页增量回填、缺页新建」与「查实配置增量补充且禁落密码明文」的方式生成受控写入计划。

## What Changes

- **Schema 领域路由解析与候选发现（`adapters/wiki/`）**：`WikiSchemaLoader` 与 `router.py` 读取 `.wiki-schema.md` 的 `content_contract.deposit_routing` 机器块（缺失时向后兼容，保持现有行为不变），动态发现候选项目已开启的领域分区目录及既有页面名摘要（例如表结构页清单、配置页清单），供 P03 规划「已有页增量更新 vs 缺页新建」。
- **P02 领域事实线索抽取（`adapters/runners/prompts.py` + `normalizers.py`）**：在 P02 审查输出契约中新增可选 `domain_signals`（覆盖 `database_schema` / `project_config` / `architecture` / `algorithm` 四域），引导审查器提取会话中真实查证过的表名/字段/枚举/边界坑、需跨会话记住的配置事实（不含凭据明文）以及架构与算法口径；未命中的域留空，不编造。
- **P03 渐进完善写入规划（`adapters/runners/prompts.py` + `core/pipeline/`）**：当挂载 Wiki 声明了 `deposit_routing` 且 P02 产出命中的 `domain_signals` 时，P03 提示词注入该契约的触发规则与目标项目既有领域页清单，指导模型在保留主经验卡名额的前提下，按「一表一页、已有页增量回填、缺页新建」与「查实配置增量补充」规划对应领域的 `knowledge_doc` 写入项（显式声明目标领域、`action=create|update` 与目标相对路径），并强制经过既有隐私/密钥门禁与人工审批门。
- **P04 领域页受控写入与安全边界（`adapters/wiki/gate.py` + `adapters/knowledge.py`）**：为领域增量更新提供确定性追加/合并校验与回读核验；命中 `project-config` 等敏感配置页的写入项 MUST 强制触发敏感写入审批（`is_sensitive_write`），并继续由 `SECRET_PATTERNS` 拦截任何密码/密钥明文，严禁自动覆盖已有章节或泄露凭据。

## Capabilities

### Modified Capabilities
- `session-review-contract`: P02 审查输出契约新增可选 `domain_signals`，提取会话中真实查证的业务表结构线索、需记忆的配置事实、架构链路与业务口径。
- `write-plan-contract`: P03 输入注入挂载 Wiki 声明的 `deposit_routing` 规则与候选项目既有领域页清单；提示词明确「渐进完善、一表一页、已有页增量回填、不得以未做全表 DDL 或无配置修改跳过」的规划规则。
- `knowledge-write-gate`: 动态解析 `.wiki-schema.md` 的 `content_contract.deposit_routing` 与项目领域页候选集，校验领域页路由声明、增量更新边界与敏感配置页审批/密钥拦截护栏。

## Impact

- 后端：`backend/ai_flywheel/adapters/wiki/schema.py`、`backend/ai_flywheel/adapters/wiki/router.py`、`backend/ai_flywheel/adapters/wiki/gate.py`、`backend/ai_flywheel/adapters/wiki/validator.py`、`backend/ai_flywheel/adapters/runners/prompts.py`、`backend/ai_flywheel/adapters/runners/normalizers.py`、`backend/ai_flywheel/models.py`、`backend/ai_flywheel/core/pipeline/coordinator.py`
- 实现期间补充触及的后端文件（上述能力落地所必需，见 `design.md` 第 4 节）：`backend/ai_flywheel/adapters/wiki/domain_pages.py`（新增：领域页按模板构建、增量无损合并与版面速写的纯函数）、`backend/ai_flywheel/adapters/knowledge.py`（落盘前对磁盘现状再合并、敏感页严格密钥检查）、`backend/ai_flywheel/core/privacy.py`（严格读取与中文凭据赋值形态）、`backend/ai_flywheel/core/pipeline/helpers.py`（路由结论落到计划项、领域页名提取）、`backend/ai_flywheel/adapters/runners/outcomes.py` / `heuristic.py` / `clients.py`（`domain_signals` 归一化接入、领域页名额排序）、`backend/ai_flywheel/adapters/runners/__init__.py` 与 `backend/ai_flywheel/adapters/wiki/__init__.py`（导出）
- 测试：`tests/test_p02_review_contract.py`、`tests/test_p03_plan_contract.py`、`tests/test_wiki_route_declaration.py`、`tests/test_wiki_gate_and_metrics.py`、`tests/test_arch_invariants.py`
- 兼容性：未在 `.wiki-schema.md` 中声明 `deposit_routing` 的知识库完全保持既有仅路由经验卡的行为，零破坏性变更。

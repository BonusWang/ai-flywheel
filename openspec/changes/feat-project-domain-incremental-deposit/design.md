# Design: 项目领域知识渐进式沉淀与四域路由感知

## 1. 架构原则与取舍

1. **契约驱动，零硬编码目录名与项目名**：
   - 领域名称、触发口径、目标子目录模板与页面模板编号全部从挂载 Wiki 的 `.wiki-schema.md` `content_contract.deposit_routing` 动态读取。
   - **备选方案与取舍**：
     - 备选 A：在 `ai-flywheel` 代码里硬编码 `数据库结构/`、`项目配置/` 等固定目录名与规则文案。
     - 放弃原因：违背引擎与知识库解耦原则，且不同 Wiki 实例的目录结构与命名约定可能不同；由 `.wiki-schema.md` 声明触发语义、引擎负责动态解析与路由门禁，既能把「查过表即渐进补表、查实配置即补配置」的规则生效到后台 P00–P07 流水线，又保持引擎仓库完全通用与公开安全。

2. **增量更新优先 + 严格保留既有内容**：
   - 已有表结构页（如某业务表已有实战已证字段）或已有配置页（如 `database.md`）命中新事实时，P03 规划 `action="update"`，只追加/增量补充新查证的字段、枚举、关联键、坑或数据源映射，绝不重写或删减原文件已有章节。
   - **备选方案与取舍**：
     - 备选 B：让模型每次输出整篇覆盖内容。
     - 放弃原因：高风险丢失既有人工核定字段与历史快照；参考 `adapters/skills.py` 的无损追加思想，采用「增量段落/行块 + 写前去重 + 原子写入回读校验」更安全。

3. **敏感配置页强审批 + 密码零落盘红线**：
   - 目标落在 `project-config`（或 frontmatter 含 `sensitivity: private_sensitive`）的计划项，在 `models.is_sensitive_write()` 中统一判为敏感写入，强制进入人工审批队列（即使处于 `smart` 模式也不静默自动落盘）。
   - 同时复用 `core/privacy.py` 的 `SECRET_PATTERNS` 与前置隐私门：允许记录逻辑库名、实例映射、配置源路径指针与连库注意事项，但命中任何密码/Token/私钥明文形态时立即丢弃计划项并记 `SECRET_LEAK_BLOCKED`。

## 2. 模块改动设计

### 2.1 `adapters/wiki/schema.py` 与 `adapters/wiki/router.py`
- `WikiSchemaLoader` 新增 `deposit_routing_rules(schema) -> list[dict[str, Any]]`：读取 `content_contract.deposit_routing.domains`（缺失返回空列表）。
- `discover_practice_projects()` 扩展每个项目候选的只读元数据 `domain_inventory: dict[str, list[str]]`：按 `deposit_routing` 声明的 `{project}/<subdir>/` 扫描当前已有的 Markdown 页面基名列表（排除隐藏文件与 `archive-*`，按文件名排序并做合理条数截断），使 P03 知道哪些表/配置页已存在。
- `RoutingCandidates` 新增 `deposit_routing` 字段（含版本、总规则及各域 trigger/target/template），`to_dict()` 按需透传给 P03 提示词。
- `evaluate_route()` 扩展支持校验 `wiki_route` 中声明的领域页类型（如 `project-algorithm`、`project-architecture`、`project-config`）：
  - 当 `page_type` 属于 `deposit_routing` 声明的目标页面类型且目标路径匹配该类型在 `.wiki-schema.md` 中声明的 `path_pattern` 时，执行与经验卡同等的确定性项目一致性和工作区/内容冲突校验；
  - 针对表结构域（`knowledge_kind: ddl`），强制「一表一页」命名边界与已有页存在性校验（目标文件已存在时 `create` 自动纠偏或要求 `update`，不存在时 `update` 拒收留痕）。

### 2.2 `adapters/runners/prompts.py` 与 `adapters/runners/normalizers.py`
- **P02 审查提示词**：
  - 在 `findings[]` 输出结构中增加可选字段 `domain_signals`：
    ```json
    "domain_signals": {
      "database_tables": [{"table": "table_name", "verified_fields": ["..."], "enums": ["..."], "pitfalls": ["..."]}],
      "project_configs": [{"topic": "config_topic", "facts": ["..."], "pitfalls": ["..."]}],
      "architecture_flows": ["..."],
      "business_calibers": ["..."]
    }
    ```
  - 明确反噪声约束：只有会话中真实查证或核实过的表/配置/架构/口径才填入；未查证不编造；严禁在 `project_configs` 中摘录密码、Token 或密钥明文。
- **P03 规划提示词**：
  - 当 `routing_candidates` 包含 `deposit_routing` 时，注入该契约的渐进完善规则与候选项目 `domain_inventory`。
  - 明确规划约束：
    1. 经验卡名额优先保留，同时在 `max_write_set` 预算内为命中的领域信号规划对应分区页；
    2. 命中 `database_tables` 时按「一表一页」处理：表名已在 `domain_inventory` 中 → `action="update"` 增量回填新查证字段/枚举/坑；不在清单中 → `action="create"` 新建实战已证版（不要求全表 DDL）；
    3. 命中 `project_configs` 时优先 `action="update"` 增量补充到对应既有配置页，严禁输出凭据明文。

### 2.3 `models.py`、`adapters/wiki/gate.py` 与 `adapters/knowledge.py`
- `models.is_sensitive_write()`：命中 `project-config` 类型或目标路径位于配置分区目录的 `knowledge_doc` 统一返回 `True`，保证门户、审批门与飞书卡片判定一致。
- `adapters/wiki/gate.py` 与 `validator.py`：
  - 新增领域页（T1/T2/T3/T6/T7）轻量适配器与写前校验：核对必填 frontmatter（含 `knowledge_kind` / `sensitivity`）、路径模式、敏感配置页 `load_policy=explicit_or_task_required` 与 `default_load=false`；
  - `action="update"` 执行增量无损合并与写前去重（若待追加事实已全部存在于目标页，丢弃并留痕 `UPDATE_NO_NEW_CONTENT`），落盘仍走「临时文件 → 原子替换 → 回读校验 → D2 后检（失败回滚）」。

## 3. 真实验证方式

1. **单元测试与不变量测试（【test】）**：
   - `tests/test_p02_review_contract.py`：验证 P02 提示词含 `domain_signals` 契约且归一化器兼容缺省与非空两种输出。
   - `tests/test_p03_plan_contract.py` 与 `tests/test_wiki_route_declaration.py`：验证 `deposit_routing` 动态读取、项目 `domain_inventory` 发现、一表一页路由校验（已有页改 `update`、缺页 `create`）、以及无 `deposit_routing` 时的完全向后兼容。
   - `tests/test_wiki_gate_and_metrics.py`：验证领域页写前校验、敏感配置页 `is_sensitive_write=True` 强制审批、密钥明文拦截（`SECRET_LEAK_BLOCKED`）、增量更新无损保留原内容及 D2 失败回滚。
2. **真实沙箱端到端回放（【real】）**：
   - 在临时隔离的 Schema 治理测试 Wiki 目录（含 `.wiki-schema.md` `deposit_routing v1.1`、已有表页与配置页）上执行一次完整的 P00–P07 沙箱回放：
     - 输入包含「查证过 1 张已有表新字段 + 1 张新表关键字段 + 1 条数据源连库坑」的脱敏测试会话；
     - 核实 P02 提取出 `domain_signals`，P03 分别规划出主经验卡 + 已有表 `update` + 新表 `create` + 配置页 `update`（标为敏感待审批）；
     - 审批放行后核实 P04 原子落盘、原文件内容无损保留、D2 后检 PASS 且 `verify_repo.py --frontend` 全门禁通过。

## 4. 实现期间补充的决定（Apply）

实现与验证过程中，在不改变上述原则与 spec 的前提下补充了以下决定，均由单测钉住：

1. **领域页的构建与合并是纯函数，写入前合并两次**：新增 `adapters/wiki/domain_pages.py`（无模型调用、无时钟、无文件访问）。`router.py` 在计划期对目标页做一次干合并——增量必须能并入、重复行去重、合并后的整页必须通过挂载契约校验；`knowledge.py` 在落盘期对「此刻磁盘上的页面」再合并一次，合并结果丢了原页的行或增量并入不了就抛错，任何字节都不落盘。
2. **未声明路由的 Wiki，P03 提示词逐字节不变**：P02 归一化后每个 finding 都带 `domain_signals`，因此构建 P03 提示词时，若挂载 Wiki 未声明 `deposit_routing`（或已声明但无可路由的页面类型 / 项目），则把 `domain_signals` 移出输入且不注入领域规则，既有 API 缓存键不受影响。
3. **P03 输入新增 `domain_page_sections`**：只对审查点到名且已存在的领域页，读取其小节名与各表的表头行（不含任何数据行），使模型写出的增量行能对上既有表头——既有页常带有自拟的小节名，模型需要照抄页面自己的小节名；声明为私有的配置页同样只暴露版面。
4. **密钥门禁分层**：通用隐私门只拒绝「凭据种类」（API Key、私钥块等），命中即丢弃计划项并记 `SECRET_LEAK_BLOCKED`；`password: …`、`密码：…` 这类「凭据赋值」在通用写入路径只遮蔽不拒绝（经验卡可以提及），但进入契约声明的敏感领域页（如配置页）或自述私有的页面的增量，在路由门（P03 与 P04 复核）和落盘门按严格读取一律拒绝，拒绝信息不回显凭据。
5. **路由纠偏与留痕**：`create` 遇到已存在的领域页时改标为 `update` 并留痕 `CREATE_TARGET_EXISTS`；`update` 的目标页不存在时拒收（`update_target_missing`）；目标页页面类型与路由声明不一致时拒收（`update_target_type_mismatch`）；增量内容已全部存在时拒收（`update_no_new_content`，留痕 `UPDATE_NO_NEW_CONTENT`），磁盘文件不变。
6. **审批模式语义不变**：`smart` 模式下配置页增量停批等待人工审批；`auto` 模式仍按既有语义自动放行（需维护者显式开启，且受安全阀 `safety_valves.auto_approve_writes` 约束）。
7. **坏契约不做静默降级**：`deposit_routing` 声明无法读取或无法兑现（未声明的模板、页面类型或分区等）时判为坏契约：读 Schema 即失败，运行在 P03 失败，绝不退化为「没有领域页」。

# workflow-orchestration —— 流水线包拆分与 P04 可选 Git 提交

> 2026-11-25。本变更不改变 P00–P07 的阶段语义、副作用边界、锁不变量与 fail-run 契约；只把实现按职责拆成 `core/pipeline/` 包，并新增一个默认关闭的写入后本地 Git 提交动作。

## ADDED Requirements

### Requirement: 模块化流水线包与兼容门面
P00–P07 的实现 MUST 位于 `backend/ai_flywheel/core/pipeline/`，并按职责拆分：节点常量与错误（`constants`）、事件总线（`events`）、候选归一化与路由辅助（`helpers`）、心跳、锁检查点与终态路径（`recovery`）、阶段序列（`coordinator`）、可选的本地 Git 提交（`git_commit`）。

`backend/ai_flywheel/core/orchestrator.py` MUST 保留为兼容门面，且少于 100 行。`WorkflowOrchestrator`、`EventBroker`、`WorkflowLockError`、`ApprovalLockBusy`、`LockLostError`、`NODE_TEMPLATES`、`PLAN_SOURCE_LABELS`、`REVIEW_SOURCE_LABELS` 与 `commit_p04_written_files` MUST 从该门面导入成功，且 MUST 与从 `core/pipeline/*` 导入的是同一对象。

流水线 MUST 在调用时经该门面解析可替换协作者（`build_runner`、`run_dream_consolidation`、`rebuild_core_card`），使既有对门面模块属性的替换继续作用到流水线。

拆分 MUST NOT 改变任何阶段的副作用边界、独占锁不变量、心跳检查点与 fail-run 契约。

#### Scenario: 旧导入路径解析到同一对象
- **WHEN** 既有代码从 `ai_flywheel.core.orchestrator` 导入编排器、事件总线、节点表或错误类型
- **THEN** 导入成功，且对象与从 `ai_flywheel.core.pipeline.*` 导入的是同一个

#### Scenario: 门面上的协作者替换仍然生效
- **WHEN** 测试替换 `ai_flywheel.core.orchestrator.build_runner` 后启动一次运行
- **THEN** P02 使用被替换的执行器，替换前的导入方式与替换点都不需要改动

#### Scenario: 阶段副作用边界不变
- **WHEN** 拆分后运行一次带错题候选与分歧候选的批次
- **THEN** P02 仍只把候选留在节点载荷里，P04 才落库；失败批次的准入会话回到 `pending` 且独占锁已释放

### Requirement: P04 可选的写入后本地 Git 提交
当 `knowledge_sink.auto_git_commit` 为真时，P04 SHALL 在本批写入全部校验通过后，把本批写出的知识文档与 Skill 文件提交到它们所在的本地 Git 仓库。提交 MUST 按仓库根分组，且每次只暂存并提交本批写出的路径（`git add -- <paths>`、`git commit --only -- <paths>`）；MUST NOT 使用 `git add -A` 或 `git add .`，MUST NOT 改动同一工作区的其它已修改或未跟踪文件，MUST NOT 执行 `git push`。

仓库已配置的 `user.name` / `user.email` MUST 被保留；仅当仓库与全局配置都没有身份时，才使用中性回退身份。提交信息 MUST 含 `run_id`、审批来源与本次提交的相对文件清单。

提交成功后 MUST 追加 `p04_git_commit` 审计事件并把结果写入 P04 节点载荷；本批写入路径不在任何 Git 仓库内时 MUST 追加 `p04_git_commit_skipped`；Git 缺失或提交失败时 MUST 追加 `p04_git_commit_failed`。任何 Git 侧结果 MUST NOT 回滚已通过校验的写入，也 MUST NOT 使运行失败。该配置默认为假。

审批来源 MUST 取自批准认领事务写入的审计事件（门户或飞书）；未挂起、自动放行的批次记为 `auto`。

#### Scenario: 默认关闭不产生提交
- **WHEN** `auto_git_commit` 保持默认假，在 Git 仓库内完成一次写出知识文档的 P04
- **THEN** 仓库的提交数不变，文档留在工作区且未被暂存，P04 节点载荷没有 Git 提交结果

#### Scenario: 开启时只提交本批写入的文件
- **WHEN** `auto_git_commit` 为真，工作区同时存在一个已修改的已跟踪文件与一个未跟踪文件
- **THEN** 恰好多出一个提交，该提交只包含 P04 写出的文件；已修改的已跟踪文件仍未提交，未跟踪文件仍未跟踪

#### Scenario: 提交信息可追溯
- **WHEN** 一次开启自动提交的批次完成
- **THEN** 提交主题为 `flywheel(p04): <run_id> (<N> file(s))`，正文含 `run_id`、`approval_source` 与相对文件清单，且存在 `p04_git_commit` 审计事件

#### Scenario: 仓库外路径留痕且不阻断
- **WHEN** `auto_git_commit` 为真，而知识落点不在任何 Git 仓库内
- **THEN** 追加 `p04_git_commit_skipped` 审计事件，P04 载荷的提交状态为 `skipped`、原因为 `not_a_git_repo`，运行仍以 `PASS` 结束

#### Scenario: Git 侧失败不失败批次
- **WHEN** Git 可执行文件缺失或提交命令失败
- **THEN** 追加 `p04_git_commit_failed` 审计事件，已通过校验的写入保持不变，运行不因 Git 失败而失败

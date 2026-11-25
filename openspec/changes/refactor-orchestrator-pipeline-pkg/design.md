# Design: core/pipeline 包拆分与 P04 可选本地 Git 提交

## Context

`core/orchestrator.py` 的 1845 行里混着五类关注点：节点常量与错误、SSE 事件总线、审查候选与路由辅助、锁与终态收尾、以及 P00–P07 的阶段序列。外部对它有两处强依赖必须保住：

1. 既有导入路径 `ai_flywheel.core.orchestrator`（`runtime.py`、`routes/runs.py`、`automation.py`、`cli.py` 与大量测试）；
2. 模块级替换点：`tests/test_p02_review_contract.py` 与 `tests/test_wiki_route_declaration.py` 直接 `monkeypatch.setattr(orchestrator, "build_runner", ...)`。

同时 P04 写入缺少本地版本轨迹：回执与审计事件能回答「写了什么」，仓库历史回答不了。

## D1 拆分形状：常量 / 事件 / 辅助 / 恢复 / 编排器

**决策**：按职责拆成六个模块，`coordinator.WorkflowOrchestrator` 继承 `recovery.RecoveryMixin`，纯函数与库操作进 `helpers`。

| 模块 | 内容 | 判据 |
|---|---|---|
| `constants` | 节点表、来源标签、错误类型、失败调用遥测 | 无状态、无 I/O |
| `events` | `EventBroker` | 独立生命周期 |
| `helpers` | 候选归一化、双轨晋升、路由校验、部分写入载荷 | 可单测的确定性逻辑 |
| `recovery` | 心跳、锁检查点、收尾/失败三条终态路径 | 共用同一写回序列 |
| `coordinator` | `__init__`、节点管线、P00–P03、审批、P04–P07 | 只在这里出现阶段顺序 |
| `git_commit` | 可选本地提交 | 与 Git 的边界隔离在一个文件里 |

**备选与取舍**：
- *继续单文件 + 分区注释*：零风险，但 hot-file 冲突与评审成本不变，且无法针对候选归一化、Git 提交单独写测试。
- *按阶段拆成 `stages/p00.py … p07.py`*：阶段文件彼此通过大量实例状态耦合（P04 读 P02 载荷、P05 读 P04 结果），会产生 8 个互相引用的碎片；本次不采用。
- *事件溯源式重写*：改变行为，超出「结构收敛」范围。

## D2 兼容门面 + 调用时解析协作者

**决策**：`core/orchestrator.py` 只做再导出（48 行）。流水线内用 `_facade_module()` 在调用时通过 `importlib` 语义导入该门面，再取 `build_runner` / `run_dream_consolidation` / `rebuild_core_card`。

**备选与取舍**：
- *流水线直接 `from ... import build_runner`*：最直观，但 `monkeypatch.setattr(orchestrator, "build_runner", …)` 会作用在一个没人读的模块属性上，四个既有测试立刻失效。这正是本次重构最容易踩的坑。
- *依赖注入（构造函数传入 `build_runner`）*：最干净，但要改 `runtime.py` 与所有测试的装配方式，扩大改动面。
- *模块级 `__getattr__` 代理*：Python 的模块 `__setattr__` 不可拦截，替换仍会落在门面上而流水线读不到；不成立。

**代价**：调用时一次模块查找；门面与流水线之间存在一个运行时的反向引用。用测试固化（`test_a_facade_monkeypatch_of_build_runner_still_reaches_the_pipeline`）。

## D3 P04 Git 提交：只碰本批写入的路径

**决策**：`commit_p04_written_files()` 按 `git rev-parse --show-toplevel` 把路径分组到仓库；每个仓库执行
`git add -- <rels>` → `git diff --cached --name-only -- <rels>` 判空 → `git commit --only -m <subject> -m <body> -- <rels>`。

- `--only` 让提交跳过索引里其它已暂存内容，配合按路径的 `diff --cached` 判空，保证无关改动既不阻挡也不搭车。
- 身份：先读 `git config user.name/user.email`，缺失的字段才追加 `-c` 回退值；仓库或全局已配置时保留操作者身份。
- 超时 15 秒；`encoding="utf-8", errors="replace"`；Windows 路径统一 `as_posix()`。
- 从不 push；Git 缺失、非仓库、提交失败都返回结构化结果并留审计，不回滚 P04 已校验的写入。

**备选与取舍**：
- *`git add -A` + 普通 `commit`*：会把用户工作区的其它改动一起提交，违反「审计轨迹只覆盖本批写入」的初衷，直接排除。
- *临时 `GIT_INDEX_FILE`*：能彻底隔离索引，但需要额外维护临时索引生命周期，且在 Windows 上与钩子/长路径交互更脆；`--only` 已满足边界。
- *失败即回滚 P04 写入*：把审计附属动作升级为写入链路的一部分，违背「写入已验证」的事实，排除。

**已知边界**：知识落点被 `.gitignore` 忽略时 `git add` 会失败并记 `p04_git_commit_failed`；这是配置不匹配的诚实反馈，示例配置里写明「知识落点需在仓库内且未被忽略」。

## D4 审批来源取自认领审计事件

**决策**：`_approval_source_for(run)` 读取该运行最近一条 `approved` 审计事件的 `payload.source`；没有该事件（自动放行）时返回 `auto`。`approved` 事件由 `claim_approval` 的同一写事务产生，一个运行只可能被认领一次。

**备选与取舍**：
- *给 `_execute_post_approval` 加 `approval_source` 形参并从 `start_run` / `resume_claimed_run` 透传*：会改变调用签名；`tests/test_wave1a_pipeline_boundary.py` 用 `_patched(run_record, simulate_fail_node=None)` 包装该方法并转到原函数，新增关键字参数会让包装器抛 `TypeError`，把两个既有用例变成假失败。
- *在实例上暂存来源*：编排器是运行时单例，跨运行残留状态不可接受。

## D5 配置与默认值

`KnowledgeSinkConfig.auto_git_commit: bool = False`。放在知识落点配置下，因为它描述的是「写出的知识文件如何留痕」，且 P04 的唯一写入目标是知识落点与 Skill 目录。默认关闭保证公开仓库与既有部署零行为变化；`flywheel.yaml.example` 用注释写明只提交本批文件、不 push、失败不致命。

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| 门面与实现漂移（有人只改门面） | 测试断言门面 < 100 行且公开符号与 `core/pipeline/*` 同一对象 |
| 调用时解析协作者被误删 | 专门的端到端测试：替换门面 `build_runner` 必须影响 P02 |
| Git 提交把无关改动带进去 | 集成测试预置「已修改的已跟踪文件 + 未跟踪文件」，断言提交只含 P04 文件、两者原样 |
| 自动提交拖慢批次 | 每次调用 15 秒超时，且仅在本批确有写入时触发；失败只留审计 |

## 真实验证方式（real）

写入链路的改动必须有 real 级证据。隔离沙箱步骤：

1. 建临时目录并 `git init`，写入 `flywheel.yaml`（`knowledge_sink.auto_git_commit: true`，`runner.mode: dry_run`，`approval_mode: auto` 且安全阀同开）。
2. 工作区预置一个已跟踪文件的修改与一个未跟踪文件。
3. `.venv/Scripts/python.exe -m ai_flywheel.cli run --base-dir <sandbox> --auto-approve`（或安装后的 `flywheel run`）。
4. 断言：运行 `PASS`；`git log` 恰好多一个提交，`git show --name-only` 只有 P04 写出的文件；两个干扰项状态不变；审计表出现 `p04_git_commit`。
5. 再以 `auto_git_commit: false` 重跑一次，确认提交数不变。

该沙箱只写临时目录，不触碰真实知识库。

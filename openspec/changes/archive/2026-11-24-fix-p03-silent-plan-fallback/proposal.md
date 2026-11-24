## Why

自 v0.2 改写 P03 提示词以来，API 模式的 5 个缓存批次里有 4 个批次的写入计划其实来自本地启发式计划器，而运行记录仍标 `api:<model>`。维护者在不知情的情况下批准了一批"以为出自模型"的写入。

- **现象**：5 个缓存批次的 23 个计划项里 16 项被丢弃（全部缺 `summary`，其中 6 项还缺 `title`），4 个批次整批改用启发式计划；落盘的是通用格式文档和 3 个只有模板话术的空壳 Skill，门户标签却是「优化已有 Skill」。
- **影响**：模型产出的规则、Skill 更新意图一条都没有进入写入；技能目录被写入空壳；没有任何日志、审计或节点标记可供事后追溯。
- **根因分三层**，缺一不可：
  1. 提示词改写（提交 `cb49225`）删掉了逐项字段骨架，模型的输出随之不带 `title` / `summary`；
  2. `_normalize_planned_write` 对校验不过的项直接返回空，不留痕；
  3. `plan_writes` 在没有保留项时改调启发式计划器，不留痕；该计划器又把重复流程候选的 `name_hint` 当成 Skill 目录。

证据见 [explore.md](./explore.md)：缓存输出回放（real）、提示词 git 历史（static）、补全后 23 / 23 项可保留（real，离线回放）。

## What Changes

- **输出契约列明必填字段**：P03 提示词与格式修复的期望结构逐项列出 `target_kind`、`action`、`title`、`summary`、`relative_path`、`source_session_keys`，并写明 Skill 更新的目标约束与空计划的返回方式。
- **可补全字段确定性补全**：缺 `title` / `summary` 的计划项按固定来源顺序补全并保留，不再丢弃；补全情况计入计划诊断。
- **丢弃留痕**：补全后仍不合规的项、超出单批上限的项，一律写入日志、P03 节点摘要与载荷、`plan_items_dropped` 审计事件。
- **BREAKING**：`api` / `cli` 模式下不再兜底。模型输出无法解析，或计划项全部无效时，P03 失败（fail-run 契约），不再改用启发式计划器。模型返回空列表视为无可写入项。
- **BREAKING**：启发式计划器不再产出 Skill 写入。重复流程候选改为留痕（节点摘要、载荷、`skill_candidates_deferred` 审计事件）。零配置演练模式因此不再自动封装 Skill。
- **计划来源写进节点**：P03 摘要与载荷写明计划来自模型、本地 CLI 还是本地启发式计划器。
- **Skill 计划项动作与目标一致**：`update` 的目标不是现有 Skill 目录时丢弃留痕，不另起新目录；`create` 的目标已存在时改标为 `update`。
- 回归测试按真实缓存输出的形状构造脱敏用例，覆盖「部分丢弃留痕」「全部丢弃不静默」「不新建模板 Skill」。

## Capabilities

### New Capabilities
- `write-plan-contract`: 写入计划契约。P03 执行器的选择、提示词与输出契约、字段补全、无效项丢弃与留痕、计划来源、启发式计划器的产出边界。前五条要求是现状基线，其余为本次增量。

### Modified Capabilities
- `workflow-orchestration`: P03 路由与写入计划。节点需写明计划来源，对丢弃项与未生成计划的 Skill 候选留痕；执行器报告计划不可用时按 fail-run 契约失败。

## Impact

- 代码：`adapters/runners.py`（输出契约、字段补全、丢弃诊断、`plan_writes_detailed`、启发式计划器）、`core/orchestrator.py`（P03 留痕与失败）。
- 测试：新增 `tests/test_p03_plan_contract.py`；`tests/test_v2_schema_and_skills.py` 中"演练模式提议 Skill"的用例按新行为改写，Skill 写入链路改由模型计划用例覆盖。
- 兼容性：无数据库迁移、无配置项变更、无 REST / MCP 接口变更。行为差异两处（见 BREAKING）：计划不可用时整轮失败；演练模式不再产出 Skill 写入。
- 不在范围：P02 审查环节的同类无留痕兜底；Skill 更新的整文件覆盖语义；经验卡归属精确性；模型耗时逼近超时。

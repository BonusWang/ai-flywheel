## 1. 回归测试先行

- [x] 1.1 按真实缓存输出的形状构造脱敏计划项夹具：缺 `summary`、缺 `title` 与 `summary`、规则项只有载荷、Skill 项带路由与 frontmatter【test】
- [x] 1.2 补全用例：缺摘要的文档、缺标题与摘要的规则、缺标题与摘要的 Skill 都被保留，补全字段进诊断【test】
- [x] 1.3 部分丢弃留痕：计划保留合规项，P03 摘要、载荷、`plan_items_dropped` 审计事件与日志齐全，节点不失败【test】
- [x] 1.4 全部丢弃不静默：P03 `FAILED`、P04–P07 `SKIPPED`、会话回 `pending`、锁释放，未调用启发式计划器，无任何写入【test】
- [x] 1.5 不新建模板 Skill：演练模式的候选只留痕；模型的 `update` 目标不存在被丢弃，`create` 目标已存在改标为 `update`【test】
- [x] 1.6 输出不可解析与空计划：前者 P03 失败，后者视为无可写入项【test】

## 2. 实现

- [x] 2.1 `runners.py`：P03 提示词与格式修复的期望结构列明必填字段、Skill 更新目标约束与空计划返回方式【test】
- [x] 2.2 `runners.py`：字段补全、丢弃诊断、Skill 动作与目标对齐、`plan_writes_detailed` 与计划不可用错误；API 与 CLI 执行器去掉 P03 兜底【test】
- [x] 2.3 `runners.py`：启发式计划器去掉 Skill 写入，产出未成计划的候选记录【test】
- [x] 2.4 `orchestrator.py`：P03 写明计划来源，对丢弃项与候选留痕（摘要、载荷、审计、日志），计划不可用时按 fail-run 契约失败【test】
- [x] 2.5 改写受影响的既有用例（演练模式不再提议 Skill），并用模型计划用例覆盖 P04 的 Skill 写入链路【test】
- [x] 2.6 `python -m pytest tests -q` 全绿、`openspec validate --all --strict` 通过、`codegraph sync`【test】
- [x] 2.7 CHANGELOG 增加条目【static】

## 3. 验证

- [x] 3.1 沙箱回放：真实编排器 + 真实缓存输出经真实执行器 + Wiki 与技能目录沙箱副本，确认计划来源为模型、补全与丢弃如实留痕、空壳 Skill 不再产生【real】
- [x] 3.2 真实知识库与真实技能目录上的完整一轮运行（维护者授权后执行：2026-11-24 15:19，`run-6e15a80207`，P00–P07 PASS，计划来源为模型；见 evidence.md 第 4 节）【real】

## Why

一次每日沉淀运行在 P04「写入核验」被 Wiki 门禁以 `FIELD_REQUIRED (type)` 拦下：整批 7 项写入中止，10 个准入会话退回 `pending`，知识库连续多批零写入。直接原因是 P03 产出的"通用知识文档"与挂载 Wiki 的经验卡合同不一致，而且不一致直到写盘前才暴露。

上一轮已加入确定性归一化，但只有简化 Schema 下的单测，没有对真实挂载 Schema 与真实 D2 后检的证据。本轮用真实编排器在 Wiki 沙箱副本回放同一批次（2026-11-24）：P04–P07 `PASS`、D2 后检 0 error，说明格式问题已解决；同时坐实归一化逻辑自身的两个缺陷：

- **项目归属靠"兜底偏好"**：无匹配时优先选目录名含 flywheel / ai 的项目。回放中 4 篇里有 3 篇来源会话的工作区与本项目无关，却全部被写成同一项目的经验卡。
- **正文映射有损且失真**：「错误示范」恒为一句固定话术，原稿里真实的异常被挤到「关键事项」，第 7 条起的要点被丢弃；`description` 与「背景与结论」直接写入原始会话 ID。

## What Changes

- **项目归属改为证据判定**（MODIFIED `通用计划文档归一化为 T5 经验卡`）：先看来源会话的工作区路径，再看草稿里的项目标识；过短的片段与通用词不再算证据。无证据时不再猜项目，返回"无法归属"；新增可选配置 `knowledge_sink.practice_fallback_project` 用于显式指定兜底项目目录。
- **正文保真映射**（ADDED `经验卡正文保真映射`）：按草稿原有小节归位，事实类要点全部保留；「错误示范」只写草稿里真实记录的异常，没有就如实写"未记录"；`description` 与「背景与结论」取草稿里的真实陈述。
- **无法归属的笔记显式跳过**（ADDED，`workflow-orchestration`）：P03 不把它列入写入计划，并在节点摘要、节点载荷与审计事件中留痕；P04 再遇到时写跳过回执，不让整批运行失败。
- 补回归测试，并以沙箱回放（真实代码 + 真实 Schema + 真实 D2 后检脚本）作为 real 级证据。

## Capabilities

### New Capabilities
无

### Modified Capabilities
- `knowledge-write-gate`: 归一化入口的项目归属判定与无法归属时的返回；新增正文保真映射要求。
- `workflow-orchestration`: P03 / P04 对无法归属项目的知识文档的处理。

## Impact

- 代码：`adapters/wiki_gate.py`（归一化与项目判定）、`core/orchestrator.py`（P03 / P04）、`config.py`（新增 `practice_fallback_project`）、`flywheel.yaml.example`（配置说明）。
- 测试：`tests/test_wiki_gate_and_metrics.py` 新增项目归属、正文保真与端到端用例。
- 兼容性：无数据库迁移、无接口变更。行为差异：未配置兜底项目时，原先会被猜进某项目的笔记改为跳过并留痕。
- 不在范围：让 P03 执行器直接按挂载 Wiki 合同产出计划文档（根因级改造，另行立项）；同日重名覆盖保护。

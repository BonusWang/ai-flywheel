## MODIFIED Requirements

### Requirement: P02 全文审查与错题入池
P02 SHALL 调用执行器审查准入会话。执行器按配置选择：`api` 模式且已配置地址与模型时用 OpenAI 兼容 API，`cli` 模式且已配置命令时用本地 CLI，否则用零成本的启发式演练执行器。审查产出的错题 MUST 按根因键写入错题观察池；仅当观察记录仍在观察中，且为高危沉默错误，或自查拦截类累计出现不少于 3 次时，才进入可晋升清单；分歧与反例 MUST 单独留存，不覆盖既有规则。

P02 MUST 在节点摘要与节点载荷里写明审查来源（模型、本地 CLI 或本地启发式审查器）。执行器的审查结果没有声明来源时，节点 MUST 写明来源未声明，不得推断。

执行器报告审查不可用时，P02 MUST 按 fail-run 契约失败：节点为 `FAILED`，节点摘要与载荷保留原因与诊断，追加 `review_unavailable` 审计事件；不得改用其他审查器，不得把任何错题或分歧写入观察池。

#### Scenario: 自查拦截类满三次进入可晋升
- **WHEN** 同一根因键的自查拦截类错题累计出现 3 次
- **THEN** 该观察记录进入本轮可晋升清单

#### Scenario: 高危沉默错误首次即可晋升
- **WHEN** 审查产出一条高危沉默错误
- **THEN** 该观察记录首次出现即进入本轮可晋升清单

#### Scenario: 模型审查不可用时整轮失败
- **WHEN** `api` 模式下模型的 P02 回复在格式修复后仍不是 JSON 对象
- **THEN** P02 为 `FAILED`，P03–P07 为 `SKIPPED`，运行为 `FAILED`，准入会话回到 `pending`，独占锁已释放；节点摘要与载荷写明原因；存在 `review_unavailable` 与 `run_failed` 审计事件；错题观察池没有新增记录，知识库与技能目录没有任何新增文件

#### Scenario: 模型没有给出任何发现
- **WHEN** `api` 模式下提示词里有会话，模型返回空的 `findings`
- **THEN** P02 为 `FAILED`，诊断的原因是没有发现，失败信息给出缓存文件名

#### Scenario: 审查来源写进节点
- **WHEN** `runner.mode` 为 `api` 且模型返回可用的审查
- **THEN** P02 为 `PASS`，节点摘要写明审查来源为 API 模型，节点载荷的 `review_source` 为 `model`

#### Scenario: 演练模式标明审查来源
- **WHEN** `runner.mode` 为 `dry_run`
- **THEN** P02 摘要写明审查来自本地启发式审查器，节点载荷的 `review_source` 为 `heuristic`

#### Scenario: 执行器没有声明来源
- **WHEN** 执行器的审查结果里没有来源
- **THEN** P02 照常通过，节点摘要写明审查来源未声明

# model-call-reliability Specification

## Purpose
模型调用的可靠性：请求参数在 OpenAI 兼容端点之间的可移植性（推理强度默认不发送）、传输失败（超时与连接错误）的即时重试与不重试边界（HTTP 错误不重试）、每次调用的用量与耗时留痕，以及失败时脱敏且可定位的错误摘要。
## Requirements
### Requirement: 请求参数的可移植性
API 执行器 SHALL 按配置决定是否发送推理强度：`runner.api.reasoning_effort` 为空时 MUST NOT 在请求 JSON 里出现推理强度字段；非空时 MUST 发送该字段的值。推理强度 MUST 进入输出缓存键：改变推理强度 MUST NOT 命中按其他推理强度产生的缓存。

#### Scenario: 未配置推理强度时不发送该参数
- **WHEN** `runner.api.reasoning_effort` 为空
- **THEN** 请求 JSON 里没有推理强度字段

#### Scenario: 配置后发送该参数
- **WHEN** `runner.api.reasoning_effort` 为 `low`
- **THEN** 请求 JSON 里带上该值

#### Scenario: 改推理强度不复用旧缓存
- **WHEN** 同一提示词在两种不同的推理强度下各调用一次
- **THEN** 两次使用不同的缓存文件

### Requirement: 传输失败的即时重试
API 执行器 SHALL 在超时或连接错误时按 `runner.api.transport_retries` 立即重试，MUST NOT 退避（超时不是限流，等待不会让下一次更快）；该值为 0 时 MUST NOT 重试。重试次数用尽后 MUST 抛出同类型的异常。HTTP 状态错误 MUST NOT 重试，并 MUST 脱敏为 `Model API returned HTTP <状态码>`，不得带上请求地址。

#### Scenario: 超时后重试成功
- **WHEN** 第一次调用读超时、第二次成功
- **THEN** 第二次的回复被采用，调用次数为 2

#### Scenario: 重试次数用尽仍失败
- **WHEN** 每次调用都超时且重试次数已用尽
- **THEN** 抛出超时异常，本批按 fail-run 契约失败

#### Scenario: 关闭重试时只调用一次
- **WHEN** `runner.api.transport_retries` 为 0 且调用超时
- **THEN** 只发起一次调用并抛出超时异常

#### Scenario: 连接错误同样重试
- **WHEN** 第一次调用连接失败、第二次成功
- **THEN** 第二次的回复被采用

#### Scenario: HTTP 错误不重试
- **WHEN** 网关返回 502
- **THEN** 只发起一次调用，错误为 `Model API returned HTTP 502`，不含请求地址

### Requirement: 调用留痕
每次模型调用 MUST 记录并随节点载荷暴露：尝试次数、耗时毫秒、实际服务的模型、输入 tokens、输出 tokens 与推理 tokens。推理 tokens SHALL 优先取 `usage.completion_tokens_details.reasoning_tokens`，没有时取 `usage.reasoning_tokens`，都没有时记为缺省。网关未给出用量、或调用被替换为桩时 MUST NOT 因此报错，留痕缺省即可。诊断与载荷 MUST NOT 包含回复正文。

#### Scenario: 节点载荷带调用留痕
- **WHEN** 一次成功的模型调用完成
- **THEN** P02 与 P03 的节点载荷带尝试次数、耗时、实际服务的模型与 token 用量

#### Scenario: 网关不给用量时不报错
- **WHEN** 回复里没有用量字段
- **THEN** token 记为缺省，调用正常完成

#### Scenario: 推理 tokens 有两种取法
- **WHEN** 用量把推理 tokens 放在明细里或放在顶层
- **THEN** 留痕里的推理 tokens 取到该值

### Requirement: 传输失败的脱敏摘要
模型调用因超时或连接错误失败时，系统 MUST 在失败节点的载荷留下脱敏摘要：阶段、超时阈值、尝试次数与异常类型。MUST NOT 留下网关地址、请求内容或回复内容；节点错误信息同样 MUST NOT 出现网关地址。

#### Scenario: 超时失败留下可定位的摘要
- **WHEN** P02 的模型调用超时且重试用尽
- **THEN** 失败节点载荷带阶段、超时阈值、尝试次数与异常类型，且不含网关地址

#### Scenario: 失败信息不含网关地址
- **WHEN** 模型调用失败且失败原因里带有网关主机名
- **THEN** 节点载荷与节点错误信息里都没有该主机名


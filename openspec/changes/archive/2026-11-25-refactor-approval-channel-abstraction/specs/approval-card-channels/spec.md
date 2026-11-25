# approval-card-channels —— 审批通道契约与实现分层

> 2026-11-25。本变更不改变飞书通道的发送形态、重试、回写与安全默认，只把「通道契约」与「具体实现的位置」写成可验证的约定。

## ADDED Requirements

### Requirement: 审批通道契约
系统 SHALL 以一个显式的审批通道契约描述编排器与运行时所依赖的全部通道能力：可用性查询（`availability`）、提问卡首次投递（`first_send`）、点击受理（`handle_click`）、启动（`start`）、停止（`stop`）与发件箱轮询（`tick`）。编排器 MUST 只通过该契约触发提问卡投递，MUST NOT 依赖某个具体通道实现的类型或模块。任一通道实现 MUST 同时提供上述全部能力；缺少任一能力的对象 MUST NOT 被装配为审批通道。

#### Scenario: 飞书通道满足契约
- **WHEN** 运行时装配了飞书审批通道
- **THEN** 该对象通过契约检查，编排器在运行挂起时经 `first_send` 投递提问卡

#### Scenario: 不完整的实现不被当作通道
- **WHEN** 某个对象缺少契约要求的任一能力
- **THEN** 契约检查不通过，该对象不会被装配为审批通道

### Requirement: 通道实现的存放与兼容导入
具体通道实现（卡片渲染、通道协调、点击监听、链路自检）MUST 位于适配器层，MUST NOT 位于领域引擎层。既有 `core.feishu_card`、`core.feishu_channel`、`core.feishu_check`、`core.feishu_listen` 导入路径 MUST 继续可用，且 MUST 解析到与适配器层完全相同的对象。

#### Scenario: 旧导入路径仍可用
- **WHEN** 既有代码从 `core.feishu_*` 导入卡片渲染器、通道类、自检服务或监听器
- **THEN** 导入成功，且对象与从 `adapters.channels.feishu.*` 导入的是同一个

#### Scenario: 实现只在适配器层
- **WHEN** 维护者查找通道实现
- **THEN** 卡片、通道、监听与自检的实现在适配器层，领域引擎层只保留再导出

### Requirement: 门户 API 契约不变
将门户 API 拆分为按业务域组织的路由模块后，全部既有 URL、HTTP 方法、状态码与响应结构 MUST 保持不变；应用 MUST 继续在 `/mcp` 挂载 MCP Streamable HTTP，并 MUST 在未命中 API 时回落到 SPA。

#### Scenario: 路由表与拆分前一致
- **WHEN** 完成路由拆分后创建应用
- **THEN** 其注册的应用路由集合（方法 + 路径）与拆分前逐条一致

#### Scenario: 错误语义不变
- **WHEN** 客户端请求一个不存在的运行、规则、知识文档或 Skill
- **THEN** 状态码与 `detail` 语义与拆分前一致

### Requirement: 通道忙时的契约化反馈
审批通道在续接运行遇到独占锁被占用时 MUST 依据契约级忙碌错误识别该结果，并保持卡片待决、提示另一批次正在执行；MUST NOT 依赖编排器模块的具体异常类型。

#### Scenario: 另一批次持锁
- **WHEN** 审批人点击卡片时另一个运行持有未过期的独占锁
- **THEN** 通道识别为忙碌，卡片保持待决并提示可稍后再点，并追加一条说明原因的审计事件

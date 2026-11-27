# distill-queue-pipeline 变更规格

## ADDED Requirements

### Requirement: Antigravity 会话 JSONL 原生解析与静默窗口采集
系统 SHALL 原生支持解析 Antigravity 客户端生成的 `transcript.jsonl` 文件并纳入会话扫盘与队列读取：
- 提取文本时 MUST 仅保留人类与模型主对话步骤（`type == "USER_INPUT"` 归一化为 `[user]`，`type == "PLANNER_RESPONSE"` 归一化为 `[assistant]`），并跳过 `GENERIC`、`CHECKPOINT`、`SYSTEM` 等工具输出或状态记录，防止原始工具日志造成数倍文本膨胀；
- 元数据提取 MUST 能从 `brain/<uuid>/.system_generated/logs/transcript.jsonl` 路径归一化规范 `session_id = <uuid>`（即 `session_key = antigravity:<uuid>`），并剥离 `<USER_REQUEST>` / `<ADDITIONAL_METADATA>` 包装标签后提取首条有效用户输入作为人类可读标题；
- 被动扫盘对 `antigravity` 源 MUST 遵守可配置静默窗口（默认 `900` 秒）：末次修改时间距当前不足静默窗口的活跃会话不得入队，避免在对话进行中半途截断沉淀。

#### Scenario: Antigravity transcript 剥离 GENERIC 工具块并提取规范身份与标题
- **WHEN** 扫盘或队列读取器加载一个包含 `USER_INPUT`、`PLANNER_RESPONSE` 与多条大体积 `GENERIC` 工具输出的 `brain/<uuid>/.system_generated/logs/transcript.jsonl`
- **THEN** 提取出的对话文本必须只包含 `[user]` 与 `[assistant]` 轮次，不得包含 `GENERIC` 工具转储内容
- **AND** 会话键必须归一化为 `antigravity:<uuid>`，可读标题必须剥离 `<USER_REQUEST>` 标签并反映首条真实用户请求

#### Scenario: 正在活跃写入的 Antigravity 会话在静默窗口内不被被动扫盘采集
- **WHEN** 一个 Antigravity `transcript.jsonl` 文件的修改时间距当前仅 60 秒（小于静默窗口 900 秒）
- **THEN** 被动扫盘对账必须跳过该活跃会话，不将其登记入队

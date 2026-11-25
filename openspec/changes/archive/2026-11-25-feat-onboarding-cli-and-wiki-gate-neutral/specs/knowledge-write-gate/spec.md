# knowledge-write-gate —— 写后钩子的中性配置名

> 2026-11-25。本变更为写入门禁的写后钩子增加中性的配置名，`wiki_d2_postflight_script` 与 `FLYWHEEL_WIKI_D2_POSTFLIGHT` 100% 继续可用；写前校验、原子写入、回滚与索引同步等既有行为不变。

## ADDED Requirements

### Requirement: 写后钩子的中性配置名与遗留别名
`knowledge_sink` MUST 提供中性配置名 `postflight_hook_script`（环境变量 `FLYWHEEL_POSTFLIGHT_HOOK_SCRIPT`），用于声明契约根目录写入后的索引/校验钩子，使非 Wiki 的 Schema 治理知识库不必沿用 Wiki 术语。遗留配置名 `wiki_d2_postflight_script`（环境变量 `FLYWHEEL_WIKI_D2_POSTFLIGHT`）MUST 保持完全兼容。

系统 MUST 以「有效写后钩子」为唯一读取口径：取 `postflight_hook_script` 去除首尾空白后的值，为空时取 `wiki_d2_postflight_script` 去除首尾空白后的值。两者同时配置时中性名优先；空白值 MUST 视同未配置。

写后执行、回滚后的索引重同步、以及「要求脚本但未配置」的失败判定 MUST 全部读取该有效值：任一个配置名都能驱动同一个钩子，行为与只配置遗留名时完全一致。有效值为空且 `wiki_gate_require_scripts` 为真时，写后检查 MUST 仍以脚本缺失判定失败并回滚（错误码 `D2_SCRIPT_MISSING`）。

#### Scenario: 中性名优先
- **WHEN** `postflight_hook_script` 与 `wiki_d2_postflight_script` 同时配置了不同脚本
- **THEN** 有效写后钩子为 `postflight_hook_script` 的值

#### Scenario: 回落到遗留名
- **WHEN** 只配置了 `wiki_d2_postflight_script`
- **THEN** 有效写后钩子为该遗留值，写后检查照常运行

#### Scenario: 环境变量配置中性名
- **WHEN** 设置 `FLYWHEEL_POSTFLIGHT_HOOK_SCRIPT` 而未在 `flywheel.yaml` 中配置任一名字
- **THEN** 加载后的 `postflight_hook_script` 为该环境变量的值，有效写后钩子取该值

#### Scenario: 两个名字都为空时失败关闭
- **WHEN** 契约根目录的写入需要写后钩子（`wiki_gate_require_scripts` 为真）而两个配置名都为空
- **THEN** 写后检查以 `D2_SCRIPT_MISSING` 失败并回滚本次写入

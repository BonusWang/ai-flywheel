# Tasks: feat-onboarding-cli-and-wiki-gate-neutral

## 1. 实现

- [x] 1.1 新增 `backend/ai_flywheel/core/doctor.py`：`run_doctor(cfg)` 只读诊断七组检查（config / storage / knowledge_sink / skills / runner / safety_valves / channels），每项 `name`/`status`/`summary`/`detail`，整体 `ok` 当且仅当零 `FAIL`；不建目录、不建库、不写配置【test】
- [x] 1.2 `cli.py` 增加 `flywheel doctor [--json]`：状态表或 JSON，`ok` 为真退出 0、否则退出 1；配置加载失败时给出可读错误【test】
- [x] 1.3 新增 `backend/ai_flywheel/core/onboarding.py`：四个客户端的格式/默认传输/惯例路径；`build_mcp_entry`（http 远程条目、stdio `flywheel mcp-stdio`）；JSON 与 TOML 的合并、重解析核对、备份、原子 LF 写入；默认 dry-run 零写入【test】
- [x] 1.4 `cli.py` 增加 `flywheel init-client <client> [--transport] [--apply] [--target] [--json]`，失败时非零退出【test】
- [x] 1.5 `KnowledgeSinkConfig` 新增 `postflight_hook_script`、`effective_postflight_script`（中性名优先、回落遗留名）与 `FLYWHEEL_POSTFLIGHT_HOOK_SCRIPT` 环境变量映射【test】
- [x] 1.6 `adapters/wiki/postflight.py` 的写后执行、回滚重同步与脚本缺失判定改读 `effective_postflight_script`，错误信息点名两个配置名【test】
- [x] 1.7 `flywheel.yaml.example` 在 `knowledge_sink` 下文档化 `postflight_hook_script`，并注明 `wiki_d2_postflight_script` 是 100% 兼容的遗留别名【static】

## 2. 测试

- [x] 2.1 新增 `tests/test_onboarding_cli.py`：doctor 健康默认配置 `ok=True` 且零写盘、api 缺 `base_url` 与缺写后钩子的 `FAIL`/退出 1、`--json` 可解析、auto→smart 降级提示、交互式飞书缺审批人 `FAIL`【test】
- [x] 2.2 同文件：init-client dry-run 零写入（含父目录）、JSON 合并保留兄弟 server 与顶层键并留备份、TOML 合并通过 `tomllib.loads` 且保留其它段并留备份、二次 `--apply` 零变化、引号段就地更新、坏 JSON / 坏 TOML 失败关闭且字节不变、Claude Desktop 拒绝 http；别名优先级 / 回落 / 环境变量；`run_postflight` 以真实 bash 子进程分别用中性名与遗留名跑通【test】
- [x] 2.3 运行 `.venv/Scripts/python.exe -m pytest tests/test_onboarding_cli.py tests/test_wiki_gate_and_metrics.py tests/test_arch_invariants.py -q`（结果：49 passed）【test】

## 3. 验证

- [x] 3.1 运行 `.venv/Scripts/python.exe -m ai_flywheel.cli doctor --json` 并记录退出码与结论（结果：退出码 0，`ok=true`，对着本机真实 `flywheel.yaml` 得到 7 PASS / 0 WARN / 0 FAIL）【real】
- [x] 3.2 运行 `.venv/Scripts/python.exe -m ai_flywheel.cli init-client claude-code --json`（仅 dry-run，不写客户端配置）并记录目标路径与条目（结果：退出码 0，`mode=dry-run`、`changed=false`、无备份；目标为用户级 Claude Code 配置文件，检测到既有 server 与 23 个顶层键将被保留，`ai-flywheel` 条目为 `http://127.0.0.1:8765/mcp`）【real】
- [x] 3.3 运行 `openspec validate --all --strict`（结果：14 passed, 0 failed）【test】
- [x] 3.4 运行 `.venv/Scripts/python.exe scripts/verify_repo.py`，hygiene / openspec / pytest 全绿（结果：hygiene 281 files clean、openspec 通过、pytest 427 passed，3/3 通过）【test】

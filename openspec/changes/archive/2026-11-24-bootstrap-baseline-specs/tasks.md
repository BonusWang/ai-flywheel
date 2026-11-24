## 1. 项目上下文与约定

- [x] 1.1 写入 `openspec/config.yaml` 的项目上下文与本仓库 OpenSpec 使用约定【static】
- [x] 1.2 确认 `openspec/config.yaml` 与两份基线规格不含个人绝对路径、私有项目名与密钥【static】

## 2. 基线规格核验

- [x] 2.1 对照 `adapters/knowledge.py`、`adapters/wiki_gate.py`、`core/privacy.py` 与 `tests/test_wiki_gate_and_metrics.py` 核验 `knowledge-write-gate` 各条需求【static】
- [x] 2.2 对照 `core/orchestrator.py`、`adapters/runners.py` 与 `tests/test_v3_true_flywheel.py` 核验 `workflow-orchestration` 各条需求【static】
- [x] 2.3 记录当前测试快照 `python -m pytest tests -q`【test】

## 3. 归档前核验

- [x] 3.1 确认本变更未改动任何代码：`git status --short` 只含 `openspec/`、`.claude/`、`.codegraph/`【static】
- [x] 3.2 `openspec validate --all --strict` 通过【test】

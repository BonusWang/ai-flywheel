## 0. 前置条件

- [x] 0.1 确认变更 `feat-p03-wiki-contract-routing` 已不再修改 `runners.py` 与 `orchestrator.py`；否则先向维护者说明并等待【static】

## 1. 回归测试先行

- [x] 1.1 可用性判定用例：无法解析、空回复、缺 `findings` 列表、空发现、没有会话时空列表正常、JSON 数组回复【test】
- [x] 1.2 失败用例：P02 `FAILED`、P03–P07 `SKIPPED`、会话回 `pending`、锁释放，未调用启发式审查器，错题观察池、知识库与技能目录没有任何变化【test】
- [x] 1.3 留痕用例：P02 摘要、载荷、`review_unavailable` 审计事件与日志齐全；缓存文件名进失败信息；诊断不含回复正文【test】
- [x] 1.4 来源用例：模型、本地 CLI、本地启发式审查器三种来源，以及执行器未声明来源【test】
- [x] 1.5 基线用例：未配置模型时用启发式审查器、第二次调用命中缓存、格式修复只重试一次【test】

## 2. 实现

- [x] 2.1 `runners.py`：审查来源、`ReviewDiagnostics`、`ReviewUnavailableError`，以及把模型 / CLI 回复变成审查结果的函数【test】
- [x] 2.2 `runners.py`：API 与 CLI 执行器去掉 P02 兜底；启发式审查器在结果里写明来源；`_invoke_api` / `_invoke_cli` 记录回复长度、解码报错与格式修复【test】
- [x] 2.3 `orchestrator.py`：P02 写明审查来源；审查不可用时留痕并按 fail-run 契约失败【test】
- [x] 2.4 改写 `tests/test_p03_plan_contract.py` 里桩模型的 P02 回复（空发现改为一条发现）【test】
- [x] 2.5 `python -m pytest tests -q` 全绿、`openspec validate --all --strict` 通过、`codegraph sync`【test】
- [x] 2.6 CHANGELOG 增加条目【static】

## 3. 验证

- [x] 3.1 沙箱回放：真实编排器 + 桩模型，修复前后对照，确认审查不可用时 P02 失败、零入池、零写入，留痕齐全【real】
- [x] 3.2 离线回放：6 份真实缓存输出经修复后的 API 执行器，确认可用的审查原样返回、来源为模型【real】
- [ ] 3.3 真实知识库与真实模型上的一轮完整运行（维护者授权后执行；授权前不勾选）【real】
  - 2026-11-24 已授权并执行一轮（`run-4483f1fe5e`）：P02 因模型调用超时失败，没有走到审查结果，不勾选。见 evidence.md 第 9 节

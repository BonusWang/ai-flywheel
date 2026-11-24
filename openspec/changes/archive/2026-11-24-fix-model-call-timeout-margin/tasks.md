## 1. 配置（`config.py` + `flywheel.yaml.example`）

- [x] 1.1 `ApiRunnerConfig` 增加 `reasoning_effort: str = ""`（为空则不发送该参数）与 `transport_retries: int = 1`（0 关闭）【test】
- [x] 1.2 示例配置补齐两项并写明含义，与代码默认值一致【static】

## 2. 调用与重试（`adapters/runners.py`）

- [x] 2.1 `_call_chat_completion` 的签名与返回类型不变；请求体按配置带 `reasoning_effort`【test】
- [x] 2.2 超时与连接错误按 `transport_retries` 立即重试、不退避；HTTP 状态错误不重试，仍脱敏为 `Model API returned HTTP <状态码>`【test】
- [x] 2.3 调用留痕：尝试次数、耗时毫秒、实际服务的模型、输入/输出/推理 tokens；网关不给用量或调用被替换为桩时缺省而不报错【test】
- [x] 2.4 推理强度进入输出缓存键，改推理强度不复用旧回复【test】

## 3. 诊断与节点载荷（`adapters/runners.py` + `core/orchestrator.py`）

- [x] 3.1 P03 与 P02 对齐：`reply_trace` 进 `_invoke_api` / `_invoke_cli`，计划诊断带回复长度、格式修复、解析错误、修复调用的异常类型与调用留痕【test】
- [x] 3.2 `P02_REVIEW.payload` 与 `P03_ROUTE_PLAN.payload` 暴露 `call_telemetry`【test】
- [x] 3.3 传输失败时节点载荷带脱敏摘要（阶段、超时阈值、尝试次数、异常类型），不含网关地址；节点错误同样不含地址【test】

## 4. 回归与归档前核验

- [x] 4.1 新增 `tests/test_model_call_reliability.py`：请求参数、缓存键、重试与不重试、留痕、节点载荷、失败摘要【test】
- [x] 4.2 改写 `tests/test_p02_review_contract.py` 中审查诊断的字段集合【test】
- [x] 4.3 `python -m pytest tests -q` 全绿、`openspec validate --all --strict` 通过【test】
- [x] 4.4 CHANGELOG 增加条目【static】

## 5. 真实验证

- [ ] 5.1 沙箱回放：真实执行器 + 桩模型确认超时重试用尽后仍按 fail-run 契约失败（下游 `SKIPPED`、会话回 `pending`、锁释放）【real】
- [ ] 5.2 真实执行器在沙箱数据目录上跑一次 P03，节点载荷里的 tokens 与耗时与网关日志一致（需维护者授权一次模型调用）【real】

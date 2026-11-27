# Tasks: feat-project-domain-incremental-deposit

- [x] 1. 在 `adapters/wiki/schema.py` 与 `adapters/wiki/router.py` 中新增 `.wiki-schema.md` `content_contract.deposit_routing` 解析与项目既有领域页清单（`domain_inventory`）动态发现，并保持未声明 `deposit_routing` 时完全向后兼容【test】
- [x] 2. 扩展 `adapters/runners/prompts.py` 与 `adapters/runners/normalizers.py` 的 P02 审查契约，支持可选 `domain_signals`（表结构查证线索、需记忆配置事实、架构链路、业务口径）的提取、归一化与反编造约束【test】
- [x] 3. 扩展 `adapters/runners/prompts.py` 与 `core/pipeline/coordinator.py` 的 P03 规划契约：注入 `deposit_routing` 渐进完善规则与 `domain_inventory`，引导按「一表一页、已有页增量回填、缺页新建」与「查实配置增量补充」生成领域 `knowledge_doc` 计划项，并保留主经验卡名额【test】
- [x] 4. 在 `adapters/wiki/router.py`、`adapters/wiki/gate.py`、`adapters/wiki/validator.py` 与 `adapters/knowledge.py` 中落地领域页路由声明校验、无损增量更新合并（含重复内容丢弃留痕）及 D2 写后校验与失败回滚【test】
- [x] 5. 更新 `models.py` 的 `is_sensitive_write()` 及相关审批测试，确保命中敏感配置页（`project-config` / `private_sensitive`）的写入项在审批门、门户与卡片三端统一判为敏感待审批，并继续由隐私门拦截密码/密钥明文【test】
- [x] 6. 补充跨层回归单测（`test_p02_review_contract.py`、`test_p03_plan_contract.py`、`test_wiki_route_declaration.py`、`test_wiki_gate_and_metrics.py`、`test_arch_invariants.py`）并在隔离测试 Wiki 上完成 P00–P07 真实端到端沙箱回放与 `python scripts/verify_repo.py --frontend` 全门禁验收【real】

## 1. 回归测试先行

- [x] 1.1 新增项目归属用例：工作区证据、内容证据、无证据不猜、通用词不算证据、兜底项目配置【test】
- [x] 1.2 新增正文保真用例：真实异常进错误示范、未记录不编造、要点不丢、描述不含会话编号【test】
- [x] 1.3 新增端到端用例：无法归属的笔记在 P03 移出计划并留痕，运行 PASS【test】

## 2. 实现

- [x] 2.1 `config.py` 新增 `knowledge_sink.practice_fallback_project`，并更新 `flywheel.yaml.example` 说明【test】
- [x] 2.2 `wiki_gate.py`：归一化入口改为证据判定项目归属，无证据返回"无法归属"；正文改为按草稿小节保真映射【test】
- [x] 2.3 `orchestrator.py`：P03 移出无法归属的计划并留痕；P04 遇到时写跳过回执【test】
- [x] 2.4 `python -m pytest tests -q` 全绿【test】

## 3. 真实验证

- [x] 3.1 用真实编排器在 Wiki 沙箱副本回放失败批次的已批准写入：运行 PASS、D2 后检 0 error、无关笔记未写入【real】
- [x] 3.2 真实知识库上的完整一轮沉淀运行通过 P04【real】（维护者授权后执行：2026-11-24 #1，P00–P07 PASS，3 篇写入，D2 后检 0 error；暴露的 4 个范围外问题见 evidence.md 第 7 节）

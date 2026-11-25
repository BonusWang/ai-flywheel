# Design: 治理与进化控制面 Web 控制台升级

## 上下文与设计约束

- 本次变更属于 P0 阶段，目标是用**最小后端侵入、零热文件修改**的方式，把仓库已具备的治理与抗熵增能力全面透出到 Web 控制台，迅速建立与 OpenViking / Memory 工具迥异的产品心智。
- 不修改 `core/orchestrator.py`、`core/feishu_*.py`、`adapters/runners/*`、`storage/db/*`，避免与当前在途的飞书交互审批修复产生冲突。

## 关键设计决策

### 1. 只读蒸馏预览（`preview_dream_consolidation`）
- **方案 A（采用）**：在 `core/dream.py` 抽离只读查询逻辑为 `preview_dream_consolidation(cfg, db, ...)`，在内存中基于 `db.list_rules()`、`db.get_knowledge_freshness_map()`、`db.list_skill_assets()`、`db.list_dissent_records()` 计算候选休眠规则、超期经验卡、合并建议与 Skill 告警，零 DB 写入。
- **方案 B（放弃）**：用 SQLite 事务回滚模拟 dry-run。容易与并发请求/审计锁冲突且违反只读清晰性。
- **取舍**：方案 A 纯只读、零副作用，且和 `run_dream_consolidation` 共用规则重合度、Skill 告警和 `proposals` 组装辅助函数，保证预览与实跑口径完全一致。

### 2. 知识新鲜度融合方式
- **方案 A（采用）**：在 `GET /api/site/knowledge` 中调用一次 `rt.db.get_knowledge_freshness_map()`，按 `doc_path` 左连接合并到返回的 `documents` 字典中；未登记指标的文档给安全默认值（`freshness_tier: "evolving"`, `is_expired: False`, `surface_count: 0`, `helped_count: 0`）。
- **方案 B（放弃）**：单独新增 `/api/site/knowledge/freshness` 让前端两次请求再手动拼接。
- **取舍**：方案 A 一次请求拿齐、完全向后兼容既有前端字段，减少网络往返。

### 3. 前端信息架构与去重
- `HomePage.tsx` 并行拉取 `api.automations()`、`api.growthMetrics()`、`api.coreCard()` 与 `api.listKnowledge()`（各自容错降级，不阻塞首屏），彻底消除硬编码 `每天 20:00`，并把治理 KPI + 待办行动置顶。
- `ConnectPolicyPage.tsx` 保留 `workflow.approval_mode` 与 `safety_valves`（且安全阀置顶），移除重复的 `schedule_enabled` / `friday_weekly_enabled` 编辑控件，改为跳转「自动化调度」的引导条，消除双入口状态漂移。
- `RulesRetroPage.tsx` 用 5 个紧凑 PillTab（`避坑规则` / `错题与分歧` / `Skill 与保鲜` / `证据台账` / `执行规矩`）+ 页头「核心索引卡」弹窗，避免新增侧边栏一级入口，守住 `5+1` 极简骨架。

## 验证计划

1. **自动化测试（`tests/test_control_plane_console.py`）**：
   - 验证 `GET /api/site/skills` 只读返回实效账本与探测数；
   - 验证 `GET /api/site/knowledge` 合并过期经验卡的 `is_expired` / `freshness_tier` / `helped_count`；
   - 验证 `GET /api/site/dream` 与 `POST /api/site/dream/run`（`dry_run=true`）严格零副作用；
   - 验证 `POST /api/site/dream/run`（`dry_run=false`）执行休眠与过期标记、落审计并刷新核心索引卡。
2. **全量门禁与真实构建**：
   - 运行 `python scripts/verify_repo.py --frontend`（hygiene + openspec + pytest + `npm run build`）。
   - 启动 `TestClient` / 本地 `serve` 对新旧接口做真实请求冒烟核验。

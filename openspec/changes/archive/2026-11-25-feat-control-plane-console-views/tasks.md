# Tasks: 治理与进化控制面 Web 控制台升级

## 1. 后端只读治理接口与蒸馏预览
- [x] 1.1 在 `backend/ai_flywheel/core/dream.py` 抽离共享候选计算辅助函数并新增零副作用 `preview_dream_consolidation()` 【test】
- [x] 1.2 在 `backend/ai_flywheel/app.py` 新增 `GET /api/site/skills`、`GET /api/site/dream`、`POST /api/site/dream/run`，并在 `GET /api/site/knowledge` 中融合经验卡新鲜度与助益字段 【test】

## 2. 前端 API / 类型与 P0-1 控制面首页和导航重塑
- [x] 2.1 在 `frontend/src/types.ts` 与 `frontend/src/api.ts` 补齐 `SkillAssetRecord`、`OutcomeRecord`、`DreamSummaryPayload`、扩展 `KnowledgeDocSummary` 及对应客户端方法（含 `refreshCoreCard`、`listOutcomes`、`listSkills`、`getDream`、`runDream`） 【static】
- [x] 2.2 更新 `frontend/src/App.tsx` 侧边栏与移动端 `5+1` 导航文案为「今日态势 / 沉淀与审批 / 自动化调度 / 规则与进化 / 接入与设置 + 关于」，并向 `ConnectPolicyPage` 透传 `onNavigate` 【static】
- [x] 2.3 重构 `frontend/src/pages/HomePage.tsx` 为「今日态势」治理指挥舱：消除硬编码调度时间、展示真实证据通过率/预检/规则健康/观察池/调度与安全阀实况、核心索引卡快捷入口及带新鲜度标签的资产列表 【real】
- [x] 2.4 优化 `frontend/src/pages/ConnectPolicyPage.tsx`：安全阀与审批策略置顶、移除与「自动化调度」重复的定时开关并替换为跳转链接、未启用飞书时弱化折叠 【static】

## 3. 前端 P0-2 治理资产、噪音漏斗、遥测与蒸馏面板
- [x] 3.1 升级 `frontend/src/pages/RulesRetroPage.tsx`：新增「Skill 与保鲜」「证据台账（Outcomes）」Tab 与页头「核心索引卡（`FLYWHEEL-CORE.md`）」实时预览及一键刷新弹窗 【real】
- [x] 3.2 升级 `frontend/src/pages/WorkflowsPage.tsx`：展开批次时展示 P00→P02→P03 噪音过滤漏斗条，节点详情透出模型调用遥测徽章（模型、耗时、Token、缓存命中） 【static】
- [x] 3.3 升级 `frontend/src/pages/AutomationsPage.tsx`：新增「周五分层蒸馏控制卡」，展示最近蒸馏摘要与建议列表，支持「预览蒸馏建议」与「立即执行蒸馏」 【real】

## 4. 回归测试、规格校验与全量验收
- [x] 4.1 新增 `tests/test_control_plane_console.py` 覆盖 `/api/site/skills`、`/api/site/knowledge` 新鲜度融合、`/api/site/dream` 与 `/api/site/dream/run`（`dry_run=true/false`） 【test】
- [x] 4.2 运行 `openspec validate --all --strict` 与 `.\.venv\Scripts\python.exe scripts/verify_repo.py --frontend` 全绿，并执行本地服务真实接口冒烟验证 【real】

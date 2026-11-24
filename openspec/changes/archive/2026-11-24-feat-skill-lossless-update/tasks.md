## 1. 写入器（`adapters/skills.py`）

- [x] 1.1 `inspect_existing_skill`：只读检视现有 Skill——存在与否、是否合规、错误说明、行数、二级标题清单、行尾、全文；路径解析严格位于技能根目录之内，围栏代码块里的标题不计入清单【test】
- [x] 1.2 `normalize_update_addition`：剥掉多写的 frontmatter 与一级标题；没有二级标题时包进 `## {标题}（{当天} 补充）`；二级标题带一次日期后缀；重复归一化结果相同【test】
- [x] 1.3 `write_skill_verified(action="update")`：目标必须存在（缺失时报 `Cannot update missing skill`）；校验现有 frontmatter；沿用原行尾；现有字节原样保留为新文件前缀；写前备份、临时文件原子替换、回读 SHA-256；回执带 `existing_lines` / `added_lines` / `total_lines` / `update_mode="append_section"`【test】
- [x] 1.4 `write_skill_verified(action="create")`：行为不变——写完整 `SKILL.md`、LF 归一、SHA-256 回读【test】
- [x] 1.5 去掉死代码 `effective_action = "create" if action != "update" else "create"`【static】

## 2. 计划归一化与提示词（`adapters/runners.py`）

- [x] 2.1 P03 提示词输入增加 `existing_skill_sections`（Skill 名 → 已有二级标题），只列本批重复流程候选命中的 Skill【test】
- [x] 2.2 P03 输出契约区分 `create`（完整 `SKILL.md`）与 `update`（只写追加到末尾的增量小节），并写明不重复已有标题【test】
- [x] 2.3 更新项归一化：目标缺失丢弃（`UPDATE_TARGET_MISSING`）、目标不合规丢弃（`UPDATE_TARGET_INVALID`）、内容全重复丢弃（`UPDATE_NO_NEW_CONTENT`）【test】
- [x] 2.4 更新项的正文预览归一化为将要追加的那一小节；审批理由写明「在现有文件末尾追加小节，原有 N 行完整保留，原文件自动备份」【test】

## 3. 门户（`app.py` + `frontend/`）

- [x] 3.1 只读接口 `GET /api/site/skills/{skill_name}/preview`：返回检视结果；不存在或目录名不合法时 404【test】
- [x] 3.2 前端：`api.skillPreview` 与 `SkillPreview` 类型【static】
- [x] 3.3 审批卡：更新项显示现有文件的行数与二级标题清单，可展开「对照现有全文」；新内容标注为「新增小节（追加到文件末尾，原有内容不动）」【static】
- [x] 3.4 前端构建通过（`tsc -b && vite build`），先在沙箱目录验证，再产出 `frontend/dist`【test】

## 4. 回归与归档前核验

- [x] 4.1 新增 `tests/test_skill_lossless_update.py`：检视、归一化、写入器追加路径、三道护栏、提示词、只读接口【test】
- [x] 4.2 改写 `tests/test_p03_plan_contract.py` 中与「整文件替换」相关的断言【test】
- [x] 4.3 `python -m pytest tests -q` 全绿、`openspec validate --all --strict` 通过【test】
- [x] 4.4 CHANGELOG 增加条目【static】

## 5. 真实验证

- [ ] 5.1 沙箱回放：真实编排器 + 真实执行器 + 技能目录沙箱副本，确认落盘文件以原有字节开头、追加小节在末尾、门户预览与落盘内容一致【real】
- [ ] 5.2 真实技能目录上的一次更新（需维护者授权）：确认原文件内容完整保留、备份可用【real】

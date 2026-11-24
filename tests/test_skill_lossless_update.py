"""Skill 更新不丢内容（feat-skill-lossless-update）。

更新一个 Skill 现在是「在文件末尾追加一个小节」：现有文件的字节一个不动，新文件以现有字节
开头。模型偶尔仍会写整份文件，这时归一化剥掉 frontmatter 与一级标题；目标缺失、不合规或
追加内容与现有内容完全重复时，该项被丢弃并留痕。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_flywheel.adapters.runners import OpenAICompatibleRunner, PlanUnavailableError, _build_p03_prompt
from ai_flywheel.adapters.skills import (
    SkillWriteError,
    SkillWriterAdapter,
    extract_h2_headings,
    inspect_skill_at,
    normalize_update_addition,
)
from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.runtime import FlywheelRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]

TODAY = "2026-11-25"

EXISTING_SKILL = (
    "---\n"
    "name: release-checklist\n"
    "description: 发版前的固定检查步骤\n"
    "---\n"
    "\n"
    "# 发版检查清单\n"
    "\n"
    "## 适用范围\n"
    "\n"
    "每次发版前逐项检查版本号、变更记录与构建产物。\n"
    "\n"
    "## 强制执行链\n"
    "\n"
    "1. 核对版本号\n"
    "2. 核对构建产物\n"
)

APPENDED_SECTION = "## 治理缺口分层审计\n\n1. 先分层再定级\n2. 每层给出证据\n"


# ---------------------------------------------------------------- helpers


def _runtime(tmp_path: Path) -> tuple[FlywheelRuntime, Path]:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills_dir)]
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    return FlywheelRuntime(cfg), skills_dir


def _writer(tmp_path: Path, skills_dir: Path) -> SkillWriterAdapter:
    """A writer whose skills root is the given directory."""
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills_dir)]
    return SkillWriterAdapter(cfg)


def _write_skill(skills_dir: Path, slug: str, body: str = EXISTING_SKILL) -> Path:
    target = skills_dir / slug / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written as bytes: the file keeps exactly the line endings the fixture declares.
    target.write_bytes(body.encode("utf-8"))
    return target


def _plan_item(slug: str, *, action: str = "update", preview: str = APPENDED_SECTION) -> dict[str, Any]:
    return {
        "target_kind": "skill_improvement",
        "relative_path": f"{slug}/SKILL.md",
        "action": action,
        "title": "发版检查清单",
        "summary": "把发版前的分层审计补进现有 Skill",
        "requires_approval": True,
        "approval_reason": "现有 Skill 缺少分层审计",
        "skill_route": {"hit": True, "action": action, "target_skill": slug, "reason": "固定步骤反复执行"},
        "content_preview": preview,
    }


class _FakeModel:
    def __init__(self, planned_writes: list[Any]) -> None:
        self.plan_json = json.dumps({"planned_writes": planned_writes}, ensure_ascii=False)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        return self.plan_json


# ---------------------------------------------------------------- inspection


def test_inspect_reports_size_outline_line_ending_and_validity(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    target = _write_skill(skills_dir, "release-checklist")

    info = _writer(tmp_path, skills_dir).inspect_existing_skill("release-checklist")

    assert info["exists"] is True
    assert info["valid"] is True
    assert info["error"] is None
    assert info["skill_name"] == "release-checklist"
    assert info["line_count"] == len(EXISTING_SKILL.splitlines())
    assert info["headings"] == ["适用范围", "强制执行链"]
    assert info["eol"] == "\n"
    assert info["content"] == EXISTING_SKILL
    assert target.exists()


def test_inspect_reads_crlf_files_and_their_headings(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    target = skills_dir / "crlf-skill" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(EXISTING_SKILL.replace("\n", "\r\n").encode("utf-8"))

    info = inspect_skill_at(skills_dir, "crlf-skill")

    assert info["eol"] == "\r\n"
    assert info["headings"] == ["适用范围", "强制执行链"]
    assert info["line_count"] == len(EXISTING_SKILL.splitlines())


def test_inspect_reports_a_missing_or_invalid_target_without_raising(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    broken = skills_dir / "broken-skill" / "SKILL.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("# 没有 frontmatter 的正文\n", encoding="utf-8")

    missing = inspect_skill_at(skills_dir, "ghost-skill")
    assert missing["exists"] is False and missing["valid"] is False
    assert "no SKILL.md" in (missing["error"] or "")

    invalid = inspect_skill_at(skills_dir, "broken-skill")
    assert invalid["exists"] is True and invalid["valid"] is False
    assert "frontmatter" in (invalid["error"] or "")

    illegal = inspect_skill_at(skills_dir, "../escape")
    assert illegal["exists"] is False and "Invalid skill folder name" in (illegal["error"] or "")


def test_headings_inside_fenced_code_blocks_are_not_headings():
    body = "## 真小节\n\n```\n## 代码里的假小节\n```\n\n### 三级小节\n"
    assert extract_h2_headings(body) == ["真小节"]


# ---------------------------------------------------------------- normalization


def test_normalize_strips_frontmatter_and_title_and_dates_the_section_once():
    raw = "---\nname: x\ndescription: y\n---\n\n# 标题\n\n## 治理缺口分层审计\n\n1. 内容\n"

    first = normalize_update_addition(raw, fallback_heading="兜底", today_ymd=TODAY)

    assert first.startswith("## 治理缺口分层审计（2026-11-25 补充）")
    assert "frontmatter" not in first and "name: x" not in first
    assert "# 标题" not in first
    # 幂等：再归一化一次，结果完全一样
    assert normalize_update_addition(first, fallback_heading="兜底", today_ymd=TODAY) == first


def test_normalize_wraps_a_body_without_a_level_two_heading():
    raw = "1. 先分层再定级\n2. 每层给出证据\n"

    result = normalize_update_addition(raw, fallback_heading="治理缺口分层审计", today_ymd=TODAY)

    assert result.startswith("## 治理缺口分层审计（2026-11-25 补充）")
    assert "1. 先分层再定级" in result


def test_normalize_keeps_an_already_dated_heading_unchanged():
    raw = "## 治理缺口分层审计（2026-11-25 补充）\n\n1. 内容\n"

    result = normalize_update_addition(raw, fallback_heading="兜底", today_ymd=TODAY)

    assert result.startswith("## 治理缺口分层审计（2026-11-25 补充）")
    assert result.count("补充）") == 1


# ---------------------------------------------------------------- the writer


def test_update_appends_and_keeps_the_old_bytes_verbatim(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    target = _write_skill(skills_dir, "release-checklist")
    original = target.read_bytes()
    writer = _writer(tmp_path, skills_dir)

    receipt = writer.write_skill_verified(
        "release-checklist/SKILL.md",
        APPENDED_SECTION,
        action="update",
        run_id="run-1",
        fallback_title="发版检查清单",
    )

    updated = target.read_bytes()
    assert updated.startswith(original), "现有文件的字节必须原样保留在新文件的开头"
    assert original.decode("utf-8") in updated.decode("utf-8")
    assert "## 治理缺口分层审计" in updated.decode("utf-8")
    assert receipt["action"] == "update"
    assert receipt["update_mode"] == "append_section"
    assert receipt["existing_lines"] == len(EXISTING_SKILL.splitlines())
    assert receipt["total_lines"] > receipt["existing_lines"]
    assert receipt["added_lines"] == receipt["total_lines"] - receipt["existing_lines"]
    assert receipt["verified"] is True
    # 写前备份
    assert receipt["backup_path"] and Path(receipt["backup_path"]).read_bytes() == original


def test_update_keeps_the_original_line_ending(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    target = skills_dir / "crlf-skill" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(EXISTING_SKILL.replace("\n", "\r\n").encode("utf-8"))
    writer = _writer(tmp_path, skills_dir)

    writer.write_skill_verified("crlf-skill/SKILL.md", APPENDED_SECTION, action="update", fallback_title="x")

    raw = target.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "追加的行也要用原来的行尾"
    assert raw.startswith(EXISTING_SKILL.replace("\n", "\r\n").encode("utf-8"))


def test_update_of_a_missing_skill_never_creates_one(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    writer = _writer(tmp_path, skills_dir)

    with pytest.raises(SkillWriteError) as excinfo:
        writer.write_skill_verified("ghost-skill/SKILL.md", APPENDED_SECTION, action="update")

    assert "Cannot update missing skill 'ghost-skill'" in str(excinfo.value)
    assert not (skills_dir / "ghost-skill").exists()


def test_update_of_an_invalid_target_is_refused(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    broken = skills_dir / "broken-skill" / "SKILL.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("# 没有 frontmatter\n", encoding="utf-8")
    writer = _writer(tmp_path, skills_dir)

    with pytest.raises(SkillWriteError):
        writer.write_skill_verified("broken-skill/SKILL.md", APPENDED_SECTION, action="update")

    assert broken.read_text(encoding="utf-8") == "# 没有 frontmatter\n"


def test_create_still_writes_a_whole_file(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    writer = _writer(tmp_path, skills_dir)
    content = "---\nname: brand-new\ndescription: 一个新 Skill\n---\n\n# 新 Skill\n\n正文正文正文正文正文。\n"

    receipt = writer.write_skill_verified("brand-new/SKILL.md", content, action="create", fallback_title="新 Skill")

    written = (skills_dir / "brand-new" / "SKILL.md").read_text(encoding="utf-8")
    assert written.startswith("---\nname: brand-new")
    assert receipt["action"] == "create"
    assert receipt["update_mode"] == "create"
    assert receipt["existing_lines"] == 0


# ---------------------------------------------------------------- P03 normalization


def test_plan_update_previews_the_exact_appended_section(tmp_path: Path):
    rt, skills_dir = _runtime(tmp_path)
    _write_skill(skills_dir, "release-checklist")
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel([_plan_item("release-checklist")])

    outcome = runner.plan_writes_detailed({"findings": [], "repeated_workflows": []}, [], 8)

    (planned,) = outcome.planned_writes
    assert planned.action == "update"
    assert planned.content_preview.startswith("## 治理缺口分层审计")
    assert "在现有文件末尾追加小节" in planned.approval_reason
    assert "整文件替换" not in planned.approval_reason
    assert f"原有 {len(EXISTING_SKILL.splitlines())} 行完整保留" in planned.approval_reason


def test_plan_update_drops_a_missing_target(tmp_path: Path):
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel([_plan_item("ghost-skill")])

    with pytest.raises(PlanUnavailableError) as excinfo:
        runner.plan_writes_detailed({"findings": [], "repeated_workflows": []}, [], 8)

    assert [d["reasons"] for d in excinfo.value.diagnostics.dropped_items] == [["UPDATE_TARGET_MISSING: ghost-skill"]]


def test_plan_update_drops_an_invalid_target(tmp_path: Path):
    rt, skills_dir = _runtime(tmp_path)
    broken = skills_dir / "broken-skill" / "SKILL.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("# 没有 frontmatter\n", encoding="utf-8")
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel([_plan_item("broken-skill")])

    with pytest.raises(PlanUnavailableError) as excinfo:
        runner.plan_writes_detailed({"findings": [], "repeated_workflows": []}, [], 8)

    assert [d["reasons"] for d in excinfo.value.diagnostics.dropped_items] == [["UPDATE_TARGET_INVALID: broken-skill"]]


def test_plan_update_drops_content_that_is_already_there(tmp_path: Path):
    rt, skills_dir = _runtime(tmp_path)
    _write_skill(skills_dir, "release-checklist")
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel(
        [_plan_item("release-checklist", preview="## 适用范围\n\n每次发版前逐项检查版本号、变更记录与构建产物。\n")]
    )

    with pytest.raises(PlanUnavailableError) as excinfo:
        runner.plan_writes_detailed({"findings": [], "repeated_workflows": []}, [], 8)

    assert [d["reasons"] for d in excinfo.value.diagnostics.dropped_items] == [["UPDATE_NO_NEW_CONTENT: release-checklist"]]


def test_plan_update_strips_a_whole_file_the_model_wrote(tmp_path: Path):
    rt, skills_dir = _runtime(tmp_path)
    _write_skill(skills_dir, "release-checklist")
    whole_file = "---\nname: release-checklist\ndescription: 被改写的描述\n---\n\n# 发版检查清单\n\n## 新增小节\n\n1. 内容\n"
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel([_plan_item("release-checklist", preview=whole_file)])

    outcome = runner.plan_writes_detailed({"findings": [], "repeated_workflows": []}, [], 8)

    (planned,) = outcome.planned_writes
    assert planned.content_preview.startswith("## 新增小节")
    assert "被改写的描述" not in planned.content_preview
    assert "frontmatter" not in planned.content_preview


# ---------------------------------------------------------------- the prompt


def test_prompt_carries_the_existing_section_outline(tmp_path: Path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "release-checklist")

    prompt = _build_p03_prompt(
        {
            "findings": [],
            "repeated_workflows": [
                {
                    "session_key": "codex:s-1",
                    "existing_skill_match": "release-checklist",
                    "action": "update",
                    "title": "发版检查",
                }
            ],
        },
        [],
        [{"name": "release-checklist", "description": "发版检查"}],
        5,
        "lessons/",
        "daily",
        existing_skill_sections={"release-checklist": ["适用范围", "强制执行链"]},
    )

    payload = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert payload["existing_skill_sections"] == {"release-checklist": ["适用范围", "强制执行链"]}
    assert "existing_skill_sections" in prompt
    # 更新项只写增量小节
    assert "ONLY the incremental Markdown section to append at the END of the existing SKILL.md" in prompt
    assert "complete valid SKILL.md" in prompt


# ---------------------------------------------------------------- the portal endpoint


def test_skill_preview_endpoint_returns_the_outline_and_404s_for_a_missing_skill(tmp_path: Path):
    rt, skills_dir = _runtime(tmp_path)
    _write_skill(skills_dir, "release-checklist")
    app = create_app(runtime=rt)

    with TestClient(app) as client:
        ok = client.get("/api/site/skills/release-checklist/preview")
        assert ok.status_code == 200
        data = ok.json()
        assert data["exists"] is True and data["valid"] is True
        assert data["headings"] == ["适用范围", "强制执行链"]
        assert data["line_count"] == len(EXISTING_SKILL.splitlines())
        assert "强制执行链" in data["content"]

        assert client.get("/api/site/skills/ghost-skill/preview").status_code == 404
        assert client.get("/api/site/skills/not%20a%20slug/preview").status_code == 404

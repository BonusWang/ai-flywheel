from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.time_utils import shanghai_today_ymd
from ai_flywheel.models import RuleStatus, utc_now_iso
from ai_flywheel.storage.db import FlywheelDB

MAX_LINES = 200
MAX_BYTES = 24 * 1024


def core_card_path(cfg: FlywheelConfig) -> Path:
    """Derived core index card always lives inside ai-flywheel's own data directory."""
    return cfg.data_path / "FLYWHEEL-CORE.md"


def render_core_card(db: FlywheelDB) -> str:
    """Render a deterministic, concise (<=200 lines, <=24 KB) cross-agent inheritance card."""
    today = shanghai_today_ymd()
    now_iso = utc_now_iso()

    rules = db.list_rules()
    active_rules = [r for r in rules if r.status in {RuleStatus.VERIFIED_ACTIVE, RuleStatus.UNVERIFIED_ACTIVE}]
    active_rules.sort(
        key=lambda r: (0 if r.status == RuleStatus.VERIFIED_ACTIVE else 1, -int(r.hit_count), r.updated_at),
    )
    dormant_count = sum(1 for r in rules if r.status == RuleStatus.DORMANT)

    open_dissents = db.get_open_dissents_by_targets([r.rule_id for r in active_rules[:25]])
    observations = db.list_mistake_observations(status="observing", limit=18)
    skills = db.list_skill_assets()
    knowledge_metrics = db.get_knowledge_card_metrics(limit=25)
    freshness_map = db.get_knowledge_freshness_map()

    lines: list[str] = [
        "# FLYWHEEL-CORE (自动派生核心索引卡)",
        "",
        f"> 生成日期：{today}（{now_iso}）｜ 派生只读视图，由 AI Flywheel 自动刷新（严禁超过 {MAX_LINES} 行 / 24 KB）",
        f"> 生效规则：{len(active_rules)} 条（另有 {dormant_count} 条休眠）｜ 观察池错题：{len(observations)} 条 ｜ 已登记 Skill：{len(skills)} 个",
        "",
        "## 1. 生效避坑规则（按验证级别与命中排序）",
    ]

    if not active_rules:
        lines.append("- 暂无生效规则。")
    else:
        for r in active_rules[:24]:
            tag = "已验证" if r.status == RuleStatus.VERIFIED_ACTIVE else "试运行"
            chk = (r.checklist[0][:64] if r.checklist else "执行前真实验证")
            d_list = open_dissents.get(r.rule_id) or []
            d_note = f" ｜ ⚠️分歧({len(d_list)}): {d_list[0]['counter_claim'][:44]}" if d_list else ""
            lines.append(f"- [{tag} · 命中{r.hit_count}] `{r.rule_id}` **{r.title[:36]}** — {chk}{d_note}")

    lines.extend(["", "## 2. 错题观察池（事不过三聚类 / 高危直通）"])
    if not observations:
        lines.append("- 观察池为空。")
    else:
        grade_badge = {"red_silent": "🔴沉默", "yellow_caught": "🟡自查", "green_minor": "🟢轻微"}
        for obs in observations[:14]:
            badge = grade_badge.get(obs.get("severity_grade", "yellow_caught"), "🟡自查")
            cnt = int(obs.get("occurrence_count", 1))
            rc = (obs.get("thinking_root_cause") or obs.get("phenomenon") or "")[:48]
            lines.append(f"- [{badge} · {cnt}/3] `{obs['root_cause_key']}` {obs['title'][:34]} — {rc}")

    lines.extend(["", "## 3. 活跃 Skill 与实效账本"])
    used_or_flywheel = [
        s for s in skills
        if int(s.get("invoke_count") or 0) > 0 or str(s.get("origin") or "").startswith("flywheel")
    ]
    shown_skills = (used_or_flywheel or skills)[:16]
    if not shown_skills:
        lines.append("- 暂无已登记 Skill。")
    else:
        for s in shown_skills:
            inv = int(s.get("invoke_count") or 0)
            ok = int(s.get("verified_pass_count") or 0)
            fail = int(s.get("fail_count") or 0)
            health = " ⚠️需检修" if fail >= 3 else ""
            desc = str(s.get("description") or "").replace("\n", " ")[:44]
            lines.append(f"- `{s['skill_name']}` (调用 {inv} / 实测通过 {ok} / 失败 {fail}{health}) — {desc}")

    lines.extend(["", "## 4. 关键经验卡与时效提醒"])
    if not knowledge_metrics:
        lines.append("- 暂无经验卡调用记录。")
    else:
        for km in knowledge_metrics[:18]:
            path = km["doc_path"]
            finfo = freshness_map.get(path) or km
            tier = finfo.get("freshness_tier") or "evolving"
            until = finfo.get("valid_until") or ""
            is_exp = bool(finfo.get("is_expired") or finfo.get("freshness_state") == "needs_recheck")
            warn = " ⚠️ [已过复核期，执行前请先核实]" if is_exp else (f" (有效期至 {until})" if until else "")
            lines.append(f"- `{path}` [{tier} · 助益{km.get('helped_count', 0)}]{warn}")

    # Enforce strict <=200 lines and <=24 KB hard cap
    if len(lines) > MAX_LINES:
        lines = lines[: MAX_LINES - 1] + ["- ...(已按 200 行硬上限截断，完整记录请通过 MCP 或控制台检索)"]

    text = "\n".join(lines).rstrip() + "\n"
    raw = text.encode("utf-8")
    if len(raw) > MAX_BYTES:
        # Trim lines from the bottom until safely under 24 KB
        footer = "\n- ...(已按 24 KB 硬上限截断)\n"
        while len(lines) > 10 and len(("\n".join(lines) + footer).encode("utf-8")) > MAX_BYTES:
            lines.pop()
        text = "\n".join(lines) + footer
    return text


def read_core_card(cfg: FlywheelConfig, db: FlywheelDB) -> dict[str, Any]:
    """Read the derived core card without touching the disk.

    The file on disk is the card; when it is missing (a fresh install, or a
    read-only mount) the card is rendered in memory instead. A plain read
    never rewrites the derived file — that is what an explicit refresh is for.
    """
    target = core_card_path(cfg)
    try:
        data = target.read_bytes()
    except OSError:
        data = None
    if data is not None:
        content = data.decode("utf-8", errors="replace")
        return {
            "path": str(target),
            "lines": content.count("\n"),
            "bytes": len(data),
            "content": content,
            "source": "file",
        }
    content = render_core_card(db)
    data = content.replace("\r\n", "\n").encode("utf-8")
    return {
        "path": str(target),
        "lines": content.count("\n"),
        "bytes": len(data),
        "content": content,
        "source": "rendered",
    }


def rebuild_core_card(cfg: FlywheelConfig, db: FlywheelDB) -> dict[str, Any]:
    """Deterministically rebuild `data/FLYWHEEL-CORE.md` using atomic temp-file swap."""
    target = core_card_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = render_core_card(db)
    data = content.replace("\r\n", "\n").encode("utf-8")
    tmp = target.with_suffix(".md.tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    line_count = content.count("\n")
    return {
        "path": str(target),
        "lines": line_count,
        "bytes": len(data),
        "content": content,
    }

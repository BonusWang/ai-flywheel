"""The card adapters: the T5 practice card and the incident card.

A planner proposes a generic draft; the adapters rewrite it into the Wiki's own page
shape. A practice card gets the frontmatter the contract declares and a body whose five
sections are filled from the draft's own lines. An incident card keeps the draft's text
under the standard section frame and completes only the metadata the engine can know.
Nothing is invented — a section the draft does not carry gets a fixed line that says so,
never a made-up fact.
"""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any

import yaml

from ai_flywheel.adapters.wiki.schema import split_frontmatter
from ai_flywheel.core.markdown_utils import parse_frontmatter, trim_blank_lines
from ai_flywheel.core.time_utils import compute_valid_until, shanghai_today_ymd
from ai_flywheel.models import INCIDENT_PAGE_TYPE

if TYPE_CHECKING:
    # Annotations only. `router` imports this module for the incident card, so this module
    # must not import `router` back at run time.
    from ai_flywheel.adapters.wiki.router import RouteDecision

MAX_DESCRIPTION_CHARS = 120
_GENERIC_SUMMARY_RE = re.compile(r"^沉淀来自\s+\S+\s+的协作经验卡$")
_LIST_MARKER_RE = re.compile(r"^(?:[-*•+]|\d+[.、)])\s+")
_TABLE_RULE_RE = re.compile(r"^\|?[\s:|\-]+\|?$")
_HR_RE = re.compile(r"^([-*_=~])\1{2,}$")
_H_ANY_RE = re.compile(r"^#{1,6}\s+")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")

# Draft H2 sections -> body buckets of the T5 practice card (keywords matched in order).
_DRAFT_SECTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("异常与根因线索", "根因线索", "错误示范", "异常", "坑"), "anomalies"),
    (("适用前提与失效条件", "适用前提", "适用范围", "前提"), "preconditions"),
    (("分歧与反例", "分歧", "反例"), "dissents"),
    (("复用建议", "复用", "建议"), "reuse"),
    (("事实回顾", "事实"), "facts"),
)
_DRAFT_BUCKETS = ("facts", "anomalies", "preconditions", "dissents", "reuse", "other")
_FRESHNESS_TIERS = {"volatile", "evolving", "evergreen"}


def _bucket_for_heading(raw: str) -> str:
    name = re.split(r"[（(]", raw, maxsplit=1)[0].strip().lower()
    for keywords, bucket in _DRAFT_SECTIONS:
        if any(keyword in name for keyword in keywords):
            return bucket
    return "other"


def _split_draft_body(raw_body: str) -> dict[str, list[str]]:
    """Bucket the draft's list items by their nearest H2 section. Nothing is synthesized."""
    buckets: dict[str, list[str]] = {name: [] for name in _DRAFT_BUCKETS}
    current = "other"
    fenced = False
    for raw in (raw_body or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced or _TABLE_RULE_RE.match(stripped) or _HR_RE.match(stripped):
            continue
        if _H2_RE.match(raw):
            current = _bucket_for_heading(stripped.lstrip("#").strip())
            continue
        if _H_ANY_RE.match(stripped):
            continue
        text = _LIST_MARKER_RE.sub("", stripped.lstrip(">").strip()).strip()
        if text and text not in buckets[current]:
            buckets[current].append(text[:180])
    return buckets


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _draft_tags(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, list):
        return [str(t).strip() for t in raw_tags if str(t).strip()][:6]
    if isinstance(raw_tags, str) and raw_tags.strip():
        return [t.strip() for t in re.split(r"[,，\s]+", raw_tags) if t.strip()][:6]
    return []


def _card_description(summary: str, buckets: dict[str, list[str]], fallback: str) -> str:
    """Retrieval line of the card: a real draft statement, never the generic sediment stub."""
    text = re.sub(r"\s+", " ", summary or "").strip()
    if not text or _GENERIC_SUMMARY_RE.match(text):
        text = next(
            (line for line in buckets["anomalies"] + buckets["facts"] + buckets["reuse"] if line),
            fallback,
        )
    return text[:MAX_DESCRIPTION_CHARS]


def _practice_card_body(clean_title: str, description: str, buckets: dict[str, list[str]], sources_list: list[str]) -> str:
    """T5 body mapped section by section from the draft: facts are kept, nothing is invented."""
    facts = _dedupe(buckets["facts"] + buckets["other"])
    preconditions = [
        line if line.startswith("适用前提") else f"适用前提与失效条件：{line}"
        for line in buckets["preconditions"][:4]
    ]
    dissents = buckets["dissents"][:3]
    reuse = [line for line in buckets["reuse"][:2] if line not in facts]

    good_flow = [f"- {line}" for line in _dedupe(facts[:4] + reuse)[:6]] or ["- 本次会话未记录可复用流程。"]
    key_facts = _dedupe(facts[4:] + preconditions) or [
        "适用前提与失效条件：适用于当前项目与工具链配置；若接口、分支策略或前置环境变更需重新核验。"
    ]
    mistakes = buckets["anomalies"][:6] or ["本次会话未记录具体的错误示范。"]
    todos = [
        f"分歧与反例：{dissents[0]}" if dissents else "分歧与反例：暂未记录反例；遇例外场景请追加记录，勿直接覆盖原结论。",
        "后续在同类真实任务中复测验证，通过后自动续期本经验卡。",
        *dissents[1:],
    ]
    body_parts = [
        f"# {clean_title}",
        "",
        f"> 背景与结论：{description}",
        "",
        "## 好流程",
        *good_flow,
        "",
        "## 错误示范",
        *[f"- {line}" for line in mistakes],
        "",
        "## 关键事项",
        *[f"- {line}" for line in key_facts],
        "",
        "## 遗留与待办",
        *[f"- {line}" for line in todos],
        "",
        "## 证据指针",
        *[f"- `{source}`" for source in sources_list],
        "",
        "> 分流判定：单项目/单事件实战经验卡，按 T5 单层规范沉淀；跨项目稳定规律经复测后再升格为主题页或全局规则。",
        "",
    ]
    return "\n".join(body_parts)


# Landing profiles of the adapter: page type -> body mapper for a generic draft. A schema page
# type is offered to the planner only when it is declared here (and see `routable_page_types`).
# The incident card is not registered here on purpose: registering it would offer the incident
# ledger for every Wiki that declares the page type, open sink prefixes or not. It is built by
# `build_incident_card` and offered only while the sink may write into the ledger.
_CARD_BODY_MAPPERS: dict[str, Any] = {
    "project-practice-note": _practice_card_body,
}


def normalize_planned_document(
    *,
    route: RouteDecision,
    relative_path: str,
    title: str,
    summary: str,
    content: str,
    projects_root_rel: str,
    source_sessions: list[str] | None = None,
) -> tuple[str, str] | None:
    """Adapt a generic P03 knowledge_doc into a schema-compliant Wiki page (T5 project-practice-note).

    Returns the ``(relative_path, markdown)`` to write, or ``None`` when the document is not
    routed: an unattributed or contradicted card is never written.
    """
    chosen = route.project
    mapper = _CARD_BODY_MAPPERS.get(route.page_type)
    if chosen is None or mapper is None:
        return None

    today_str = shanghai_today_ymd()
    clean_title = re.sub(r"[\r\n]+", " ", (title or "协作实战经验卡")).strip()[:64] or "协作实战经验卡"
    file_slug = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", clean_title).strip("-").lower()[:40] or "practice-note"
    rec_slug = re.sub(r"[^a-z0-9]+", "-", clean_title.lower()).strip("-")[:24]
    if len(rec_slug) < 6:
        # CJK-dominant titles collapse to a tiny ASCII slug; add a title digest to keep record_id unique
        digest = hashlib.sha1(clean_title.encode("utf-8")).hexdigest()[:8]
        rec_slug = f"{rec_slug}-{digest}" if rec_slug else f"card-{digest}"
    record_id = f"{today_str}-{rec_slug}"
    target_rel = f"{projects_root_rel}/{chosen['dir_name']}/实战笔记/{today_str}-{file_slug}.md"

    draft_fm, raw_body = parse_frontmatter(content or "")
    buckets = _split_draft_body(raw_body)
    description = _card_description(summary, buckets, clean_title)
    sources_list = [f"ai-flywheel session: {sk}" for sk in (source_sessions or []) if sk][:5]
    if not sources_list:
        sources_list = [f"ai-flywheel batch {today_str}"]
    tier = str(draft_fm.get("freshness_tier") or "evolving").strip().lower()
    if tier not in _FRESHNESS_TIERS:
        tier = "evolving"

    fm_obj = {
        "title": clean_title,
        "description": description,
        "type": route.page_type,
        "project_id": chosen["project_id"],
        "record_id": record_id,
        "event_date": today_str,
        "load_policy": "on_demand",
        "default_load": False,
        "created": today_str,
        "updated": today_str,
        "status": "active",
        "freshness_tier": tier,
        "last_verified": today_str,
        "valid_until": compute_valid_until(tier, today_str),
        "tags": _draft_tags(draft_fm.get("tags")) or ["实战复盘", "协作飞轮"],
        "sources": sources_list,
    }
    fm_yaml = yaml.safe_dump(fm_obj, allow_unicode=True, sort_keys=False, width=1000).strip()
    body = mapper(clean_title, description, buckets, sources_list)
    return target_rel, f"---\n{fm_yaml}\n---\n\n{body}"


# ---------------- the incident card ----------------

# The sections of an incident card, in their canonical order: the standard section frame.
INCIDENT_SECTIONS: tuple[str, ...] = ("定性", "事实核验", "根因", "治本规则", "证据指针")
# The one line a section the draft left empty carries. It says that the draft recorded
# nothing here; it is a gap the next reader fills, never a statement about the incident.
INCIDENT_EMPTY_SECTION = "（本次审查未记录这一节，待补充。）"
INCIDENT_TITLE_LIMIT = 120  # characters kept in an incident card's title
INCIDENT_DESCRIPTION_LIMIT = 200  # the contract's comfort limit for a non-practice description
# How a draft's level-2 heading names a section of the frame (keywords matched in order).
_INCIDENT_SECTION_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("定性",), "定性"),
    (("事实核验", "事实", "核验"), "事实核验"),
    (("根因", "根本原因", "原因"), "根因"),
    (("治本规则", "治本", "修复规则", "改进规则"), "治本规则"),
    (("证据指针", "证据", "参考"), "证据指针"),
)


def _incident_draft_layout(
    body: str,
) -> tuple[str, list[str], dict[str, list[str]], list[tuple[str, list[str]]]]:
    """``(title line, preamble, frame sections, other sections)`` of an incident draft's body.

    A level-2 heading opens a section: one of the frame's when its name says so (a
    parenthetical suffix is ignored), else a section of the draft's own, kept as it was
    named. The lines before the first heading are the preamble. A level-1 heading is
    dropped (the page has exactly one, and it is written from the title; the first one's
    text is returned as the title line), a code fence stays whole with the section it
    sits in, and every other line is kept as the draft wrote it.
    """
    title_line = ""
    preamble: list[str] = []
    sections: dict[str, list[str]] = {name: [] for name in INCIDENT_SECTIONS}
    others: list[tuple[str, list[str]]] = []
    current = preamble
    fenced = False
    for raw in (body or "").replace("\r\n", "\n").split("\n"):
        stripped = raw.strip()
        if stripped[:3] in {"```", "~~~"}:
            fenced = not fenced
            current.append(raw.rstrip())
            continue
        if not fenced:
            heading = _H1_RE.match(raw)
            if heading:
                title_line = title_line or heading.group(1).strip()
                continue
            heading = _H2_RE.match(raw)
            if heading:
                name = re.split(r"[（(]", heading.group(1), maxsplit=1)[0].strip()
                frame_section = next(
                    (
                        section
                        for keywords, section in _INCIDENT_SECTION_KEYWORDS
                        if any(keyword in name for keyword in keywords)
                    ),
                    None,
                )
                if frame_section is not None:
                    current = sections[frame_section]
                else:
                    current = []
                    others.append((heading.group(1).strip(), current))
                continue
        current.append(raw.rstrip())
    return (
        title_line,
        trim_blank_lines(preamble),
        {name: trim_blank_lines(lines) for name, lines in sections.items()},
        [(name, trim_blank_lines(lines)) for name, lines in others],
    )


def _incident_draft_frontmatter(fm_text: str | None) -> tuple[dict[str, Any], str]:
    """``(the draft's frontmatter, a problem)``: a mapping, or why it cannot be read."""
    if fm_text is None:
        return {}, ""
    try:
        loaded = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return {}, f"草稿的 frontmatter 无法解析：{str(exc).splitlines()[0] if str(exc) else 'YAML 错误'}"
    if not isinstance(loaded, dict):
        return {}, "草稿的 frontmatter 不是一个映射"
    return {str(key).strip() or key: value for key, value in loaded.items()}, ""


def build_incident_card(
    *,
    title: str,
    summary: str,
    content: str,
    required_fields: list[str] | tuple[str, ...],
    allowed_status: list[str] | tuple[str, ...],
    load_policy: str,
    today: str,
    source_sessions: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Assemble a page of the Wiki's incident ledger from the planner's draft.

    The draft supplies everything the card says: its frontmatter values and its text. The
    engine supplies only what it can know — the page type, the title when the draft has
    none, the plan's summary as the description, the day, the sessions the card came from
    and what the contract prescribes for every such page (the status a new page starts
    with, the load policy the page type declares). A field the contract requires and
    neither the draft nor the engine can state is left out and reported: it is never
    invented here.

    The body is the draft's own text under the standard section frame: the five sections
    in their canonical order, a section the draft did not write carrying the one line that
    says so, and a section of the draft's own kept after them. Building a card again from
    a card this built changes nothing.

    Returns ``(page, problems)``; with problems the page is empty.
    """
    fm_text, raw_body = split_frontmatter(content or "")
    draft, problem = _incident_draft_frontmatter(fm_text)
    if problem:
        return "", [problem]
    title_line, preamble, sections, others = _incident_draft_layout(raw_body)
    named = next(
        (str(value) for value in (draft.get("title"), title_line, title) if str(value or "").strip()),
        "",
    )
    page_title = re.sub(r"[\r\n]+", " ", named).strip()[:INCIDENT_TITLE_LIMIT] or "协作事故卡"

    frontmatter: dict[str, Any] = dict(draft)
    frontmatter["title"] = page_title
    frontmatter["type"] = INCIDENT_PAGE_TYPE
    if not str(frontmatter.get("description") or "").strip():
        frontmatter["description"] = " ".join(str(summary or "").split())[:INCIDENT_DESCRIPTION_LIMIT] or page_title
    if not frontmatter.get("created"):
        frontmatter["created"] = today
    if not frontmatter.get("updated"):
        frontmatter["updated"] = today
    statuses = [str(value) for value in (allowed_status or [])]
    if not frontmatter.get("status") and (not statuses or "active" in statuses):
        # `active` only when the contract lets a new incident page carry it.
        frontmatter["status"] = "active"
    if not frontmatter.get("load_policy") and load_policy:
        frontmatter["load_policy"] = load_policy
    if not frontmatter.get("sources"):
        frontmatter["sources"] = [f"ai-flywheel session: {key}" for key in (source_sessions or [])[:5]] or [
            f"ai-flywheel batch {today}"
        ]
    missing = [name for name in required_fields if frontmatter.get(name) in (None, "", [])]
    if missing:
        return "", [f"契约要求字段 {'、'.join(missing)}，草稿未给出，引擎不代填"]
    if not preamble and not others and not any(sections.values()):
        return "", ["草稿没有事故内容：标准小节均为空"]

    page_lines = [f"# {page_title}", ""]
    if preamble:
        page_lines += [*preamble, ""]
    for name in INCIDENT_SECTIONS:
        page_lines += [f"## {name}", "", *(sections[name] or [INCIDENT_EMPTY_SECTION]), ""]
    for name, lines in others:
        page_lines += [f"## {name}", "", *(lines or [INCIDENT_EMPTY_SECTION]), ""]
    fm_yaml = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False, width=1000).strip()
    return f"---\n{fm_yaml}\n---\n\n" + "\n".join(page_lines).rstrip("\n") + "\n", []

"""飞书卡片渲染与外发文本清洗（feat-feishu-approval-card-loop design D8）。

除代码常量以外全部文本进 plain_text；清洗剔除控制/格式/代理字符、折叠空白、
尖括号与命令行特殊字符全角化、链接去活化、剥离 private 片段并按密钥样式掩码。
体积按 UTF-8 字节计，超限按「摘录→摘要→理由→路径」逐级截短，绝不删写入项。

版式依据 lark-cli 内嵌的飞书卡片排版规范（lark-im-card-style）：
- header 承载「这是什么」：图标 + 副标题（批次 / 审批模式）+ 状态标签；
- body 只有一个最强焦点（首行的加粗结论），其余为支撑；
- 每条写入一个描边块（P2 分组），块内标题一行、细节小字灰字；
- 间距交给容器（body / 块的 vertical_spacing），不散设 margin；
- 主操作蓝底撑满（primary_filled + fill），其余次级（default），取消为 danger。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from ai_flywheel.core.privacy import SECRET_PATTERNS
from ai_flywheel.models import is_sensitive_write

FULLWIDTH_MAP = str.maketrans({c: chr(ord(c) + 0xFEE0) for c in "&|<>^%!"})
PRIVATE_RE = re.compile(r"<private>.*?</private>", re.S | re.I)
# The canonical secret patterns (core.privacy) mask every credential shape the product
# knows; the whole match is replaced, so no part of the secret survives in a card.
SECRET_MASK = "（疑似密钥已掩码）"
LINK_SCHEME_RE = re.compile(r"\b(h(?:tt|t)ps?)(:/{1,2})", re.I)

# Field limits per detail level. A card shrinks through the levels until it fits
# the byte budget; items are never dropped, only their detail gets shorter.
LEVELS: tuple[dict[str, int], ...] = (
    {"title": 48, "path": 120, "summary": 120, "reason": 160, "excerpt": 240},
    {"title": 48, "path": 96, "summary": 80, "reason": 100, "excerpt": 120},
    {"title": 40, "path": 72, "summary": 48, "reason": 64, "excerpt": 60},
    {"title": 32, "path": 56, "summary": 0, "reason": 0, "excerpt": 0},
)
CARD_BYTES_BUDGET = 24 * 1024
KIND_LABEL = {"knowledge_doc": "笔记", "flywheel_rule": "规则", "skill_improvement": "Skill"}
ACTION_LABEL = {"create": "新建", "update": "更新", "delete": "删除", "overwrite_core": "覆盖核心", "register_rule": "注册"}
OPTION_APPROVE_ALL = "同意并写入"
OPTION_APPROVE_ROUTINE = "仅同意常规项"
OPTION_REJECT = "取消本次写入"
APPROVAL_MODE_LABEL = {"smart": "智能审批", "always": "每次人工审批", "auto": "全部自动"}


def is_sensitive(w: dict[str, Any]) -> bool:
    """敏感情项谓词：选项、认领与渲染共用同一个判定。

    Delegates to the canonical predicate in ``models`` so the card, the approval
    gate and the portal can never drift apart.
    """
    return is_sensitive_write(w)


def sanitize(text: str, *, limit: int | None = None) -> str:
    out_chars: list[str] = []
    for ch in str(text or ""):
        cat = unicodedata.category(ch)
        if cat in {"Zs", "Zl", "Zp"} or ch in "\r\n\t":
            out_chars.append(" ")
            continue
        if cat in {"Cc", "Cf", "Cs", "Cn", "Co"}:
            continue
        out_chars.append(ch)
    folded = re.sub(r" {2,}", " ", "".join(out_chars)).strip()
    # Strip secrets and private sections while their markup is still intact,
    # then make the remaining markup / shell characters inert.
    folded = PRIVATE_RE.sub("（已省略敏感片段）", folded)
    for secret in SECRET_PATTERNS:
        folded = secret.pattern.sub(SECRET_MASK, folded)
    folded = folded.translate(FULLWIDTH_MAP)
    folded = LINK_SCHEME_RE.sub(lambda m: m.group(1) + "：/", folded)
    if not folded:
        folded = "（空）"
    if limit and len(folded) > limit:
        folded = folded[: limit - 1] + "…"
    return folded


def sha12(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8", errors="replace")).hexdigest()[:12]


# ---------------- building blocks ----------------


def _pt(raw: str, limit: int, *, size: str = "normal", color: str | None = None, lines: int = 2) -> dict:
    """Untrusted text → plain_text only (never lark_md), bounded in length and lines."""
    text: dict[str, Any] = {"tag": "plain_text", "content": sanitize(raw, limit=limit), "text_size": size, "lines": lines}
    if color:
        text["text_color"] = color
    return {"tag": "div", "text": text}


def _trusted(raw: str, limit: int, *, size: str = "normal", color: str | None = None, lines: int = 2) -> dict:
    """System-generated text (labels, URLs, counts) → plain_text without link deactivation."""
    text: dict[str, Any] = {"tag": "plain_text", "content": str(raw)[:limit], "text_size": size, "lines": lines}
    if color:
        text["text_color"] = color
    return {"tag": "div", "text": text}


def _detail(label: str, value: str, limit: int) -> dict | None:
    """「标签：值」小字一行；空值不占位（never renders a placeholder like 「（空）」)."""
    if not str(value or "").strip():
        return None
    return _pt(f"{label}：{value}", limit + len(label) + 1, size="notation", color="grey", lines=2)


def _container(elements: list[dict], *, background: str, border_color: str | None = None) -> dict:
    block: dict[str, Any] = {
        "tag": "interactive_container",
        "width": "fill",
        "background_style": background,
        "corner_radius": "8px",
        "padding": "12px 12px 12px 12px",
        "vertical_spacing": "4px",
        "elements": elements,
    }
    if border_color:
        block["has_border"] = True
        block["border_color"] = border_color
    return block


def _button(label: str, card_id: str, action: str, *, btn_type: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btn_type,
        "size": "large",
        "width": "fill",
        "behaviors": [{"type": "callback", "value": {"card": card_id, "action": action}}],
    }


def _has_button(element: dict) -> bool:
    if element.get("tag") == "button":
        return True
    return any(_has_button(child) for col in element.get("columns", []) for child in col.get("elements", []))


def _card(*, title: str, subtitle: str, template: str, icon: str, tag: tuple[str, str] | None, elements: list[dict]) -> str:
    header: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": title},
        "subtitle": {"tag": "plain_text", "content": subtitle[:60]},
        "template": template,
        "icon": {"tag": "standard_icon", "token": icon},
    }
    if tag:
        header["text_tag_list"] = [{"tag": "text_tag", "text": {"tag": "plain_text", "content": tag[0]}, "color": tag[1]}]
    return json.dumps(
        {
            "schema": "2.0",
            "config": {"update_multi": True, "enable_forward": False},
            "header": header,
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 20px 12px",
                "vertical_spacing": "8px",
                "elements": elements,
            },
        },
        ensure_ascii=False,
    )


# ---------------- ask card ----------------


def _item_block(index: int, w: dict[str, Any], *, sensitive: bool, limits: dict[str, int]) -> list[dict]:
    kind = KIND_LABEL.get(str(w.get("target_kind")), "其他")
    act = ACTION_LABEL.get(str(w.get("action")), "变更")
    accent, border, background = ("blue", "blue-100", "blue-50") if sensitive else ("grey", "grey-100", "grey-50")
    elements: list[dict] = [
        {"tag": "markdown", "content": f"**<font color='{accent}'>{index}. {kind} · {act}</font>**"},
        _pt(w.get("title", ""), limits["title"], lines=2),
    ]
    details = [
        _detail("路径", w.get("relative_path") or "", limits["path"]),
        _detail("摘要", w.get("summary") or "", limits["summary"]),
        _detail("理由", w.get("approval_reason") or "", limits["reason"]),
    ]
    if sensitive:
        excerpt = str(w.get("content_preview") or "")
        details.append(_detail("摘录（≠全文，全文在控制台）", excerpt[: limits["excerpt"] * 4], limits["excerpt"]))
        if excerpt:
            details.append(_trusted(f"内容校验：{sha12(excerpt)}", 40, size="notation", color="grey", lines=1))
    elements.extend(d for d in details if d is not None)
    return [_container(elements, background=background, border_color=border)]


def _section_title(text: str, *, accent: str) -> dict:
    return {"tag": "markdown", "content": f"**<font color='{accent}'>{text}</font>**"}


def _notice_block(notice: str) -> dict:
    return _container([_pt(notice, 240, size="notation", lines=3)], background="grey-50")


def _ask_elements(
    *,
    card_id: str,
    reasons: list[str],
    writes: list[dict[str, Any]],
    portal_url: str,
    routine_count: int,
    limits: dict[str, int],
) -> list[dict]:
    sensitive = [w for w in writes if is_sensitive(w)]
    routine = [w for w in writes if not is_sensitive(w)]
    elements: list[dict] = []
    if sensitive and routine:
        focus = (
            f"**<font color='blue'>{len(sensitive)} 项敏感情项需你确认</font>**"
            f"<font color='grey'>，另有 {len(routine)} 项常规写入将一并处理。</font>"
        )
    elif sensitive:
        focus = f"**<font color='blue'>{len(sensitive)} 项敏感情项需你确认</font>**<font color='grey'>，批准后才会写入。</font>"
    else:
        focus = f"**{len(routine)} 项写入等你确认。**<font color='grey'>批准后才会写入知识库或生效为规则。</font>"
    elements.append({"tag": "markdown", "content": focus})
    for r in reasons[:3]:
        if str(r or "").strip():
            elements.append(_pt(f"原因：{r}", 170, size="notation", color="grey", lines=2))
    if sensitive:
        elements.append(_section_title(f"需你确认 · {len(sensitive)} 项", accent="blue"))
        for i, w in enumerate(sensitive, 1):
            elements.extend(_item_block(i, w, sensitive=True, limits=limits))
    if routine:
        elements.append(_section_title(f"常规项 · {len(routine)} 项", accent="grey"))
        for i, w in enumerate(routine, 1):
            elements.extend(_item_block(i, w, sensitive=False, limits=limits))
    if routine_count:
        elements.append(_trusted(f"另有 {routine_count} 项常规笔记已随来源批次自动写入。", 80, size="notation", color="grey", lines=1))
    elements.append(_trusted(f"控制台（仅本机可打开）：{portal_url}", 200, size="notation", color="grey", lines=1))
    return elements


def _actions(card_id: str, *, total: int, routine: int, mixed: bool) -> list[dict]:
    primary = _button(f"{OPTION_APPROVE_ALL}（{total} 项）", card_id, "approve_all", btn_type="primary_filled")
    secondary = [_button(f"{OPTION_APPROVE_ROUTINE}（{routine} 项）", card_id, "approve_routine", btn_type="default")] if mixed else []
    secondary.append(_button(OPTION_REJECT, card_id, "reject", btn_type="danger"))
    if len(secondary) == 1:
        return [primary, *secondary]
    return [
        primary,
        {
            "tag": "column_set",
            "flex_mode": "bisect",
            "horizontal_spacing": "8px",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [secondary[0]]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [secondary[1]]},
            ],
        },
    ]


def render_ask_card(
    *,
    card_id: str,
    batch_label: str,
    reasons: list[str],
    writes: list[dict[str, Any]],
    portal_url: str,
    routine_count: int = 0,
    subtitle: str = "",
    notice: str = "",
) -> str:
    """提问卡：自包含（批次、原因、全部写入项、控制台地址、三个操作）。

    writes 每项：target_kind/action/relative_path/title/summary/approval_reason/content_preview。
    """
    label = sanitize(batch_label, limit=32)
    head = f"批次 {label}" if not subtitle else subtitle
    sensitive = [w for w in writes if is_sensitive(w)]
    routine = [w for w in writes if not is_sensitive(w)]
    actions = _actions(
        card_id, total=len(writes), routine=len(routine), mixed=bool(sensitive and routine)
    )
    for limits in LEVELS:
        elements = _ask_elements(
            card_id=card_id, reasons=reasons, writes=writes, portal_url=portal_url, routine_count=routine_count, limits=limits
        )
        if notice:
            elements.append(_notice_block(notice))
        card = _card(
            title="AI 飞轮待审批",
            subtitle=head,
            template="blue",
            icon="approve_colorful",
            tag=("待审批", "yellow"),
            elements=[*elements, *actions],
        )
        if len(card.encode("utf-8")) <= CARD_BYTES_BUDGET:
            return card
    # Still too large (pathological input): keep every action and say how much is waiting.
    elements = [
        {"tag": "markdown", "content": f"**<font color='blue'>{len(writes)} 项写入等你确认。</font>**<font color='grey'>内容过长已折叠，清单请在控制台查看。</font>"},
        _trusted(f"控制台（仅本机可打开）：{portal_url}", 200, size="notation", color="grey", lines=1),
    ]
    if notice:
        elements.append(_notice_block(notice))
    return _card(
        title="AI 飞轮待审批",
        subtitle=head,
        template="blue",
        icon="approve_colorful",
        tag=("待审批", "yellow"),
        elements=[*elements, *actions],
    )


def callback_button(label: str, card_id: str, action: str, *, btn_type: str = "primary_filled") -> dict:
    """A full-width large callback button (the card's main action)."""
    return _button(label, card_id, action, btn_type=btn_type)


def render_message_card(
    *,
    title: str,
    headline: str,
    lines: list[str],
    background: str,
    template: str,
    icon: str,
    tag: tuple[str, str],
    subtitle: str = "",
    button: dict | None = None,
) -> str:
    """A single-block message card: bold conclusion, optional plain lines, optional one action.

    Used for cards that are not ask cards (self-check, simple notices)."""
    block = _container(
        [{"tag": "markdown", "content": headline}, *(_pt(x, 320, lines=4) for x in lines if str(x or "").strip())],
        background=background,
    )
    elements = [block] + ([button] if button is not None else [])
    return _card(title=title, subtitle=subtitle or title, template=template, icon=icon, tag=tag, elements=elements)


def with_notice(ask_card_json: str, notice: str) -> str:
    """The delivered ask card plus a one-off notice, placed right above the actions.

    Buttons stay intact: the card is still open and must remain clickable."""
    try:
        card = json.loads(ask_card_json)
        elements = card["body"]["elements"]
    except Exception:
        return ask_card_json
    index = next((i for i, e in enumerate(elements) if _has_button(e)), len(elements))
    elements.insert(index, _notice_block(notice))
    return json.dumps(card, ensure_ascii=False)


# ---------------- state cards ----------------


def _result_block(background: str, headline: str, lines: list[str | dict]) -> dict:
    elements: list[dict] = [{"tag": "markdown", "content": headline}]
    for line in lines:
        elements.append(line if isinstance(line, dict) else _pt(line, 320, lines=4))
    return _container(elements, background=background)


def render_state_card(
    *,
    card_id: str,
    state: str,
    batch_label: str = "",
    selected: str = "",
    result_summary: str = "",
    busy_hint: str = "",
    unavailable: bool = False,
    portal_url: str = "",
) -> str:
    """已受理 / 已完成 / 执行失败 / 已取消 / 按钮不可用：结论加粗在前，不带任何按钮。"""
    subtitle = f"批次 {sanitize(batch_label, limit=40)}" if batch_label else "AI 飞轮"
    chosen = sanitize(selected, limit=24)
    summary = str(result_summary or "").strip()
    console = _trusted(f"控制台（仅本机可打开）：{portal_url}", 200, size="notation", color="grey", lines=1) if portal_url else None
    hint = _pt(busy_hint, 240, size="notation", lines=3) if busy_hint else None

    if unavailable:
        elements = [_result_block("yellow-50", "**<font color='yellow'>飞书按钮暂不可用</font>**", ["请在电脑前打开控制台批准或取消。"] + ([console] if console else []))]
        return _card(title="AI 飞轮待审批", subtitle=subtitle, template="yellow", icon="warning_outlined", tag=("需在电脑处理", "yellow"), elements=elements)

    if state == "decided":
        lines = [_trusted(f"所选操作：{chosen}", 60, size="notation", color="grey", lines=1)] if chosen else []
        lines.append("正在执行，结果会回到本卡。")
        elements = [_result_block("green-50", f"**已选择「{chosen}」**", lines)]
        if hint:
            elements.append(_container([hint], background="grey-50"))
        return _card(title="AI 飞轮待审批", subtitle=subtitle, template="green", icon="approve_colorful", tag=("已受理", "green"), elements=elements)

    if state == "done":
        lines = [_trusted(f"所选操作：{chosen}", 60, size="notation", color="grey", lines=1)] if chosen else []
        if summary:
            lines.append(summary)
        elements = [_result_block("green-50", "**<font color='green'>执行完成</font>**", lines)]
        return _card(title="AI 飞轮审批结果", subtitle=subtitle, template="green", icon="done_outlined", tag=("已完成", "green"), elements=elements)

    if state == "failed":
        lines = [summary] if summary else []
        elements = [_result_block("red-50", "**<font color='red'>执行失败</font>**", lines)]
        if console:
            elements.append(console)
        return _card(title="AI 飞轮审批结果", subtitle=subtitle, template="red", icon="warning_outlined", tag=("执行失败", "red"), elements=elements)

    elements = [_result_block("grey-50", "**<font color='grey'>已取消本次写入</font>**", ["没有写入发生。"])]
    return _card(title="AI 飞轮审批结果", subtitle=subtitle, template="grey", icon="close_outlined", tag=("已取消", "neutral"), elements=elements)

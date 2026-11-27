"""Turning a runner's reply into a plan or a review.

A model or CLI reply is untrusted input. This module decides whether it is usable at
all: a reply that carries no plan fails the node (it never falls back to the local
heuristic planner), and a reply whose items are all unusable fails it too, with the
per-item reason codes that explain why.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ai_flywheel.adapters.runners.contracts import (
    _NO_FALLBACK_HINT,
    _NO_REVIEW_FALLBACK_HINT,
    PLAN_SOURCE_CLI,
    PLAN_SOURCE_MODEL,
    REVIEW_EMPTY_FINDINGS,
    REVIEW_EMPTY_REPLY,
    REVIEW_FINDINGS_NOT_LIST,
    REVIEW_UNPARSABLE_REPLY,
    PlanDiagnostics,
    PlanOutcome,
    PlanUnavailableError,
    ReviewDiagnostics,
    ReviewUnavailableError,
    _json_error_note,
)
from ai_flywheel.adapters.runners.normalizers import (
    _item_brief,
    _normalize_plan_item,
    normalize_domain_signals,
    normalize_review_events,
)
from ai_flywheel.adapters.wiki.router import RoutingCandidates
from ai_flywheel.models import PlannedWrite

logger = logging.getLogger(__name__)

_SOURCE_NOUN = {PLAN_SOURCE_MODEL: "模型", PLAN_SOURCE_CLI: "本地 CLI "}


def _apply_reply_trace(diagnostics: PlanDiagnostics, reply_trace: dict[str, Any] | None) -> None:
    """Copy what the reply was (its length, the repair, the call cost) into the plan diagnostics."""
    if not reply_trace:
        return
    diagnostics.output_chars = reply_trace.get("output_chars")
    diagnostics.repair_attempted = bool(reply_trace.get("repair_attempted"))
    diagnostics.parse_error = reply_trace.get("parse_error")
    diagnostics.repair_parse_error = reply_trace.get("repair_parse_error")
    diagnostics.repair_error = reply_trace.get("repair_error")
    telemetry = reply_trace.get("call_telemetry")
    diagnostics.call_telemetry = dict(telemetry) if telemetry else None


def _unavailable_plan_message(diagnostics: PlanDiagnostics) -> str:
    invalid = len(diagnostics.dropped_items) - diagnostics.over_limit_count
    parts: list[str] = []
    if invalid:
        codes = sorted(
            {
                reason.split(":")[0]
                for item in diagnostics.dropped_items
                if "OVER_LIMIT" not in item["reasons"]
                for reason in item["reasons"]
            }
        )
        parts.append(f"{invalid} 项未通过校验（{'、'.join(codes)}）")
    if diagnostics.over_limit_count:
        parts.append(f"{diagnostics.over_limit_count} 项超出单批上限")
    noun = _SOURCE_NOUN.get(diagnostics.source, "模型")
    message = f"{noun}返回的写入计划 {diagnostics.returned_count} 项全部未列入（{'，'.join(parts)}），本批不写入。"
    if diagnostics.cache_file:
        message += f"该输出缓存在 {diagnostics.cache_file}，同批次重跑会复用，删除它可让模型重答。"
    return message + _NO_FALLBACK_HINT


def _is_domain_page_item(raw_item: Any, domain_page_types: frozenset[str]) -> bool:
    """Whether a plan item is a project-domain page the Wiki's deposit contract declares."""
    if not domain_page_types or not isinstance(raw_item, dict):
        return False
    route = raw_item.get("wiki_route")
    page_type = str(route.get("page_type") or "").strip().lower() if isinstance(route, dict) else ""
    return page_type in domain_page_types


def _plan_from_items(
    raw_items: list[Any],
    *,
    source: str,
    max_write_set: int,
    default_doc_prefix: str,
    skills_root: Path | None,
    cache_file: str | None = None,
    cache_hit: bool = False,
    cache_file_present: bool = False,
    reply_trace: dict[str, Any] | None = None,
    domain_page_types: frozenset[str] = frozenset(),
    max_domain_writes: int | None = None,
    total_write_set: int | None = None,
    model: str = "",
) -> PlanOutcome:
    """Normalize every item a model or CLI returned. Raises `PlanUnavailableError` when none of them is usable.

    Within the batch cap the main experience card keeps its slot: the items of a project-domain
    page (``domain_page_types``, the page types the Wiki's deposit contract declares) are read
    after every other item, whatever order the reply used, so an item beyond the cap is theirs.

    ``max_write_set`` caps the items that are not project-domain pages. When a Wiki's deposit
    contract declares domain pages, the runner also names ``max_domain_writes`` (how many of
    them this batch may plan, a quota of their own) and ``total_write_set`` (the ceiling over
    both kinds together); without them one cap covers everything, as before. An item counts
    against the quotas only once it is kept, so an invalid item never spends the slot of a
    valid one behind it.
    """
    diagnostics = PlanDiagnostics(
        source=source,
        returned_count=len(raw_items),
        cache_file=cache_file if cache_file_present else None,
        cache_hit=cache_hit,
        model=model,
    )
    _apply_reply_trace(diagnostics, reply_trace)
    planned: list[PlannedWrite] = []
    split_quotas = max_domain_writes is not None and total_write_set is not None
    domain_cap = max(0, int(max_domain_writes or 0))
    total_cap = max(1, int(total_write_set or 0))
    if split_quotas:
        diagnostics.write_caps = {"other": max(0, int(max_write_set)), "domain": domain_cap, "total": total_cap}
    kept_domain = 0
    reading_order = sorted(
        range(len(raw_items)), key=lambda index: _is_domain_page_item(raw_items[index], domain_page_types)
    )
    for index in reading_order:
        raw_item = raw_items[index]
        domain_item = _is_domain_page_item(raw_item, domain_page_types)
        # A domain page is read after every other item, so by then `planned` holds the other items kept.
        if split_quotas:
            own_quota = kept_domain >= domain_cap if domain_item else len(planned) >= max_write_set
            over_quota = len(planned) >= total_cap or own_quota
        else:
            over_quota = len(planned) >= max_write_set
        if over_quota:
            # The quota counts the writes that were kept, not the items that were read:
            # an invalid item must not use up the slot of a valid one behind it.
            diagnostics.over_limit_count += 1
            brief = _item_brief(raw_item)
            diagnostics.dropped_items.append({"index": index, **brief, "reasons": ["OVER_LIMIT"]})
            logger.info("P03 plan item not planned: index=%s kind=%s action=%s reasons=%s", index, brief["target_kind"], brief["action"], ["OVER_LIMIT"])
            continue
        pw, reasons, notes, completed = _normalize_plan_item(
            raw_item, default_doc_prefix=default_doc_prefix, skills_root=skills_root
        )
        for name in completed:
            diagnostics.completed_fields[name] = diagnostics.completed_fields.get(name, 0) + 1
        if pw is None:
            brief = _item_brief(raw_item)
            diagnostics.dropped_items.append({"index": index, **brief, "reasons": reasons})
            logger.warning(
                "P03 plan item dropped: index=%s kind=%s action=%s reasons=%s",
                index,
                brief["target_kind"],
                brief["action"],
                reasons,
            )
            continue
        planned.append(pw)
        kept_domain += 1 if domain_item else 0
        diagnostics.relabeled_skill_actions.extend(notes)
        if not pw.source_session_keys:
            diagnostics.missing_source_session_keys += 1
    diagnostics.kept_count = len(planned)

    if diagnostics.completed_fields:
        logger.info("P03 plan: completed missing fields %s", diagnostics.completed_fields)

    if raw_items and not planned:
        raise PlanUnavailableError(_unavailable_plan_message(diagnostics), diagnostics)
    # The plan is usable: the caller persists the reply right after, so the cache file exists.
    diagnostics.cache_file = cache_file
    return PlanOutcome(planned, diagnostics)


def deposit_page_types(routing_candidates: RoutingCandidates | None) -> frozenset[str]:
    """The page types of the project-domain pages a Wiki's deposit contract declares.

    Empty for a Wiki that declares none, and for a knowledge base without a contract:
    their plans are read in the order the reply used, as before.
    """
    deposit = routing_candidates.deposit_routing if routing_candidates is not None else None
    return frozenset(deposit.page_types) if deposit is not None else frozenset()


def _plan_from_runner_output(
    parsed: Any,
    *,
    source: str,
    max_write_set: int,
    default_doc_prefix: str,
    skills_root: Path | None,
    cache_file: str | None = None,
    cache_hit: bool = False,
    cache_file_present: bool = False,
    reply_trace: dict[str, Any] | None = None,
    domain_page_types: frozenset[str] = frozenset(),
    max_domain_writes: int | None = None,
    total_write_set: int | None = None,
    model: str = "",
) -> PlanOutcome:
    """Turn a parsed model/CLI reply into a plan. Never substitutes the heuristic planner."""
    if not (isinstance(parsed, dict) and isinstance(parsed.get("planned_writes"), list)):
        noun = _SOURCE_NOUN.get(source, "模型")
        diagnostics = PlanDiagnostics(
            source=source,
            cache_file=cache_file if cache_file_present else None,
            cache_hit=cache_hit,
            model=model,
        )
        _apply_reply_trace(diagnostics, reply_trace)
        raise PlanUnavailableError(
            f"{noun}未返回可解析的写入计划（缺少 planned_writes 列表），本批不写入。{_NO_FALLBACK_HINT}",
            diagnostics,
        )
    return _plan_from_items(
        parsed["planned_writes"],
        source=source,
        max_write_set=max_write_set,
        default_doc_prefix=default_doc_prefix,
        skills_root=skills_root,
        cache_file=cache_file,
        cache_hit=cache_hit,
        cache_file_present=cache_file_present,
        reply_trace=reply_trace,
        domain_page_types=domain_page_types,
        max_domain_writes=max_domain_writes,
        total_write_set=total_write_set,
        model=model,
    )


def _unavailable_review_message(diagnostics: ReviewDiagnostics) -> str:
    reasons = {
        REVIEW_EMPTY_REPLY: "回复为空",
        REVIEW_UNPARSABLE_REPLY: "回复不是 JSON 对象",
        REVIEW_FINDINGS_NOT_LIST: "回复里没有 findings 列表",
        REVIEW_EMPTY_FINDINGS: f"对 {diagnostics.sessions_sent} 个会话的审查没有任何发现",
    }
    noun = _SOURCE_NOUN.get(diagnostics.source, "模型")
    message = f"{noun}返回的审查结果不可用（{reasons.get(diagnostics.reason_code, diagnostics.reason_code)}），本批不审查、不写入。"
    if diagnostics.reason_code == REVIEW_UNPARSABLE_REPLY and diagnostics.parse_error:
        # Why the reply is not JSON, in the decoder's words; never a fragment of the reply.
        message += _json_error_note(diagnostics.parse_error)
    if diagnostics.output_chars:
        message += f"回复 {diagnostics.output_chars} 字符。"
    if diagnostics.repair_error:
        # The repair call itself failed (a timeout, a connection error): that, not a repaired reply, is what happened.
        message += f"格式修复的调用没有完成（{diagnostics.repair_error}）。"
    elif diagnostics.repair_attempted:
        message += "已做过一次格式修复。"
    if diagnostics.cache_file:
        message += f"该输出缓存在 {diagnostics.cache_file}，同批次重跑会复用，删除它可让模型重答。"
    return message + _NO_REVIEW_FALLBACK_HINT


def _review_from_runner_output(
    parsed: Any,
    *,
    source: str,
    phase: str,
    sessions_sent: int,
    output_chars: int | None,
    repair_attempted: bool,
    cache_file: str | None,
    cache_hit: bool,
    cache_file_present: bool = False,
    parse_error: str | None = None,
    repair_parse_error: str | None = None,
    repair_error: str | None = None,
    call_telemetry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn a parsed model/CLI reply into a session review. Raises `ReviewUnavailableError` when nothing was reviewed.

    ``cache_file`` names the file the caller persists this reply in once the review turns
    out to be usable; ``cache_file_present`` says whether such a file is already on disk.
    Only a file that really exists is named in an error message — an unusable reply is
    never cached, so a stale file is the only reason to mention one.
    """

    def _diagnostics(reason_code: str | None, *, finding_count: int, usable: bool) -> ReviewDiagnostics:
        return ReviewDiagnostics(
            source=source,
            sessions_sent=sessions_sent,
            finding_count=finding_count,
            output_chars=output_chars,
            repair_attempted=repair_attempted,
            parse_error=parse_error,
            repair_parse_error=repair_parse_error,
            repair_error=repair_error,
            call_telemetry=call_telemetry,
            reason_code=reason_code,
            cache_file=cache_file if (usable or cache_file_present) else None,
            cache_hit=cache_hit,
        )

    def unavailable(reason_code: str) -> ReviewUnavailableError:
        diagnostics = _diagnostics(reason_code, finding_count=0, usable=False)
        return ReviewUnavailableError(_unavailable_review_message(diagnostics), diagnostics)

    if not isinstance(parsed, dict):
        raise unavailable(REVIEW_EMPTY_REPLY if output_chars == 0 else REVIEW_UNPARSABLE_REPLY)
    findings = parsed.get("findings")
    if not isinstance(findings, list):
        raise unavailable(REVIEW_FINDINGS_NOT_LIST)
    if sessions_sent and not findings:
        # No session was reviewed. Letting it through would settle the sessions as if one had been.
        raise unavailable(REVIEW_EMPTY_FINDINGS)
    # The reply is usable: the caller persists it right after, so the cache file exists.
    diagnostics = _diagnostics(None, finding_count=len(findings), usable=True)
    for key in ("mistakes", "dissents", "repeated_workflows"):
        parsed.setdefault(key, [])
    # Optional in the review contract too: the things a session did that stand on their own
    # (a delivery, an incident, a decision, a domain fact). Absent means none was recorded.
    parsed["events"] = normalize_review_events(parsed.get("events"))
    for finding in findings:
        if isinstance(finding, dict):
            # Optional in the review contract, present afterwards: a planner reads one shape.
            finding["domain_signals"] = normalize_domain_signals(finding.get("domain_signals"))
    parsed["phase"] = phase
    parsed["sessions_reviewed"] = len(findings)
    parsed["review_source"] = source
    parsed["review_diagnostics"] = diagnostics.to_dict()
    parsed["call_telemetry"] = dict(call_telemetry) if call_telemetry else None
    return parsed

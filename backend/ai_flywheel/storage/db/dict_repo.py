"""The unified code dictionary: builtin seeds and read/write helpers.

``sys_dict_items`` is the single place the console and API read display
metadata (Chinese/English labels, grouping, ordering, colour/icon hints) from.
The Python enums stay the execution contract; the dictionary is presentation
and extensibility, seeded idempotently on every startup.

Seeding never overwrites a user extension: rows with ``is_builtin = 0`` are
left exactly as the maintainer wrote them, and only builtin rows get their
labels/metadata refreshed when they drift from the seed.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ai_flywheel.models import DictItem


def _item(
    dict_type: str,
    code: str,
    label_zh: str,
    label_en: str,
    *,
    group_key: str = "",
    description: str = "",
    sort_order: int = 100,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "dict_type": dict_type,
        "code": code,
        "label_zh": label_zh,
        "label_en": label_en,
        "group_key": group_key,
        "description": description,
        "sort_order": sort_order,
        "meta": dict(meta or {}),
    }


#: Every builtin dictionary category and item. Covers each value of the five
#: execution enums (queue status, ingress channel, run status, rule status,
#: signal kind) plus the extensible classification lists (exclude reasons,
#: client types, priorities, entity types).
BUILTIN_DICT_SEEDS: tuple[dict[str, Any], ...] = (
    # ---------------- queue_status ----------------
    _item("queue_status", "queued", "排队中", "Queued", group_key="active",
          description="等待被批次认领的沉淀任务", sort_order=10, meta={"color": "blue"}),
    _item("queue_status", "processing", "处理中", "Processing", group_key="active",
          description="已被某一批次认领、正在沉淀", sort_order=20, meta={"color": "amber"}),
    _item("queue_status", "processed", "已沉淀", "Processed", group_key="closed",
          description="已完成沉淀并写入台账", sort_order=30, meta={"color": "green"}),
    _item("queue_status", "excluded", "已排除", "Excluded", group_key="closed",
          description="按排除原因码跳过，不进入沉淀", sort_order=40, meta={"color": "gray"}),
    _item("queue_status", "withdrawn", "已撤回", "Withdrawn", group_key="closed",
          description="被用户或调用方主动撤回", sort_order=50, meta={"color": "gray"}),
    _item("queue_status", "dead_letter", "死信", "Dead letter", group_key="closed",
          description="重试耗尽后隔离，等待人工排查", sort_order=60, meta={"color": "red"}),

    # ---------------- ingress_channel ----------------
    _item("ingress_channel", "mcp_explicit", "MCP 显式入队", "MCP explicit", group_key="push",
          description="客户端通过 MCP 工具显式提交沉淀", sort_order=10, meta={"icon": "plug"}),
    _item("ingress_channel", "manual_portal", "控制台手动入队", "Manual portal", group_key="push",
          description="在网页控制台手工登记后入队", sort_order=20, meta={"icon": "edit"}),
    _item("ingress_channel", "codex_archive", "Codex 归档", "Codex archive", group_key="archive",
          description="Codex 会话归档时自动入队", sort_order=30, meta={"icon": "archive"}),
    _item("ingress_channel", "claude_exit", "Claude 退出", "Claude exit", group_key="archive",
          description="Claude 会话结束时自动入队", sort_order=40, meta={"icon": "door"}),
    _item("ingress_channel", "legacy_scan", "历史扫描", "Legacy scan", group_key="archive",
          description="历史会话批量补录扫描", sort_order=50, meta={"icon": "search"}),

    # ---------------- exclude_reason ----------------
    _item("exclude_reason", "USER_OPT_OUT", "用户主动退出", "User opt-out",
          description="用户明确表示该会话不参与沉淀", sort_order=10),
    _item("exclude_reason", "SUBAGENT_FRAGMENT", "子代理片段", "Subagent fragment",
          description="子代理的碎片输出，缺少完整上下文", sort_order=20),
    _item("exclude_reason", "EXCLUDED_WORKSPACE", "工作区被排除", "Excluded workspace",
          description="工作区命中排除规则", sort_order=30),
    _item("exclude_reason", "TOO_SHORT", "内容过短", "Too short",
          description="有效内容低于最小长度阈值", sort_order=40),
    _item("exclude_reason", "TEMP_CHAT_LOW_SIGNAL", "临时闲聊低信号", "Low-signal temp chat",
          description="临时对话且没有可沉淀的事实", sort_order=50),
    _item("exclude_reason", "SINGLE_TURN_LOOKUP", "单轮查询", "Single-turn lookup",
          description="一次性的查问，无需沉淀", sort_order=60),
    _item("exclude_reason", "MANUAL_EXCLUDE", "人工排除", "Manual exclude",
          description="维护者在控制台手工排除", sort_order=70),
    _item("exclude_reason", "EMPTY_DELTA", "无增量", "Empty delta",
          description="相对已消费水位线没有任何新增内容", sort_order=80),
    _item("exclude_reason", "SUPERSEDED", "已被新版本取代", "Superseded",
          description="同一会话的更新 revision 已覆盖该任务", sort_order=90),

    # ---------------- client_type ----------------
    _item("client_type", "codex", "Codex", "Codex", group_key="ide",
          description="Codex CLI 接入端", sort_order=10),
    _item("client_type", "claude-code", "Claude Code", "Claude Code", group_key="ide",
          description="Claude Code 接入端", sort_order=20),
    _item("client_type", "claude", "Claude Desktop", "Claude Desktop", group_key="desktop",
          description="Claude Desktop 接入端", sort_order=30),
    _item("client_type", "antigravity", "Antigravity", "Antigravity", group_key="ide",
          description="Antigravity 接入端", sort_order=40),
    _item("client_type", "portal-sandbox", "自测模拟", "Portal sandbox", group_key="internal",
          description="门户自测与演练的模拟接入端", sort_order=50),
    _item("client_type", "generic", "通用", "Generic", group_key="internal",
          description="未标注具体接入端的通用条目", sort_order=60),

    # ---------------- priority_level ----------------
    _item("priority_level", "P0_CORE", "P0 核心", "P0 core", group_key="high",
          description="核心决策与硬约束，最先认领", sort_order=10, meta={"color": "red"}),
    _item("priority_level", "P1_SIGNAL", "P1 信号", "P1 signal", group_key="high",
          description="明确的问题信号与复盘线索", sort_order=20, meta={"color": "amber"}),
    _item("priority_level", "P1_GENERAL", "P1 常规", "P1 general", group_key="normal",
          description="常规沉淀任务", sort_order=30, meta={"color": "blue"}),
    _item("priority_level", "P2_META", "P2 元信息", "P2 meta", group_key="low",
          description="低优先级的元信息与背景材料", sort_order=40, meta={"color": "gray"}),

    # ---------------- run_status ----------------
    _item("run_status", "PENDING", "待启动", "Pending", group_key="open",
          description="批次已登记但尚未开始", sort_order=10),
    _item("run_status", "RUNNING", "执行中", "Running", group_key="open",
          description="批次正在执行 P00-P07", sort_order=20),
    _item("run_status", "AWAITING_APPROVAL", "等待放行", "Awaiting approval", group_key="open",
          description="写入计划已生成，等待人工放行", sort_order=30),
    _item("run_status", "PASS", "通过", "Pass", group_key="terminal",
          description="批次完整通过", sort_order=40),
    _item("run_status", "PARTIAL", "部分完成", "Partial", group_key="terminal",
          description="部分写入完成", sort_order=50),
    _item("run_status", "CANCELLED", "已取消", "Cancelled", group_key="terminal",
          description="审批门被拒，整批零写入", sort_order=60),
    _item("run_status", "FAILED", "失败", "Failed", group_key="terminal",
          description="批次执行失败并已回滚状态", sort_order=70),

    # ---------------- rule_status ----------------
    _item("rule_status", "unverified_active", "试运行待验证", "Unverified active", group_key="active",
          description="已生效但尚无自然验证样本", sort_order=10),
    _item("rule_status", "verified_active", "已验证生效", "Verified active", group_key="active",
          description="已有真实通过样本支撑", sort_order=20),
    _item("rule_status", "needs_revision", "需修订", "Needs revision", group_key="active",
          description="证据显示规则需要调整", sort_order=30),
    _item("rule_status", "dormant", "已休眠", "Dormant", group_key="inactive",
          description="长时间未命中，暂时休眠", sort_order=40),
    _item("rule_status", "retired", "已停用", "Retired", group_key="inactive",
          description="被新规则取代或明确停用", sort_order=50),

    # ---------------- signal_kind ----------------
    _item("signal_kind", "retry_loop", "重复报错", "Retry loop",
          description="同一错误反复重试", sort_order=10),
    _item("signal_kind", "high_risk_tool", "高风险操作", "High-risk tool",
          description="调用了高风险工具或命令", sort_order=20),
    _item("signal_kind", "missing_evidence", "缺少证据", "Missing evidence",
          description="结论缺少可核验的执行证据", sort_order=30),
    _item("signal_kind", "rule_recurrence", "问题再次出现", "Rule recurrence",
          description="已沉淀的问题再次发生", sort_order=40),
    _item("signal_kind", "blocker", "执行受阻", "Blocker",
          description="任务被环境或依赖阻断", sort_order=50),
    _item("signal_kind", "custom", "自定义信号", "Custom",
          description="调用方自定义的信号种类", sort_order=60),

    # ---------------- entity_type ----------------
    _item("entity_type", "queue_item", "队列项", "Queue item",
          description="distill_queue 中的一条沉淀任务", sort_order=10),
    _item("entity_type", "session", "会话", "Session",
          description="sessions 中的一条会话主档", sort_order=20),
    _item("entity_type", "workflow_run", "批次", "Workflow run",
          description="workflow_runs 中的一次 P00-P07 运行", sort_order=30),
    _item("entity_type", "rule", "规则", "Rule",
          description="rules 中的一条规则", sort_order=40),
    _item("entity_type", "decision_card", "决策卡", "Decision card",
          description="decision_cards 中的一张审批或询问卡", sort_order=50),
    _item("entity_type", "mistake_observation", "错题观察", "Mistake observation",
          description="mistake_observations 中的一条错题", sort_order=60),
    _item("entity_type", "dissent_record", "分歧记录", "Dissent record",
          description="dissent_records 中的一条分歧", sort_order=70),
)


class DictRepoMixin:
    """The unified ``sys_dict_items`` dictionary: seeds, reads and upserts."""

    def _seed_builtin_dicts(self, conn: sqlite3.Connection) -> None:
        """Idempotently plant the builtin dictionary items.

        Missing rows are inserted; existing builtin rows get their labels and
        metadata refreshed only when they drift from the seed. Rows with
        ``is_builtin = 0`` are never inserted over, updated or deleted, so a
        maintainer's custom client type or exclude reason survives every start.
        """
        now = self._now_iso()
        for seed in BUILTIN_DICT_SEEDS:
            dict_type = str(seed["dict_type"])
            code = str(seed["code"])
            meta_json = json.dumps(seed.get("meta") or {}, ensure_ascii=False)
            conn.execute(
                """
                INSERT OR IGNORE INTO sys_dict_items (
                    dict_type, code, label_zh, label_en, group_key, description,
                    sort_order, is_builtin, enabled, meta_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
                """,
                (
                    dict_type,
                    code,
                    seed["label_zh"],
                    seed["label_en"],
                    seed["group_key"],
                    seed["description"],
                    int(seed["sort_order"]),
                    meta_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM sys_dict_items WHERE dict_type = ? AND code = ?",
                (dict_type, code),
            ).fetchone()
            if row is None or not row["is_builtin"]:
                continue
            current = (
                row["label_zh"],
                row["label_en"],
                row["group_key"],
                row["description"],
                int(row["sort_order"]),
                row["meta_json"],
            )
            desired = (
                seed["label_zh"],
                seed["label_en"],
                seed["group_key"],
                seed["description"],
                int(seed["sort_order"]),
                meta_json,
            )
            if current == desired:
                continue
            conn.execute(
                """
                UPDATE sys_dict_items SET
                    label_zh = ?, label_en = ?, group_key = ?, description = ?,
                    sort_order = ?, meta_json = ?, updated_at = ?
                WHERE dict_type = ? AND code = ? AND is_builtin = 1
                """,
                (
                    seed["label_zh"],
                    seed["label_en"],
                    seed["group_key"],
                    seed["description"],
                    int(seed["sort_order"]),
                    meta_json,
                    now,
                    dict_type,
                    code,
                ),
            )

    @staticmethod
    def _row_to_dict_item(row: sqlite3.Row) -> DictItem:
        return DictItem(
            dict_type=row["dict_type"],
            code=row["code"],
            label_zh=row["label_zh"],
            label_en=row["label_en"],
            group_key=row["group_key"],
            description=row["description"],
            sort_order=int(row["sort_order"]),
            is_builtin=bool(row["is_builtin"]),
            enabled=bool(row["enabled"]),
            meta=json.loads(row["meta_json"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_dicts(
        self, dict_type: str | None = None, include_disabled: bool = False
    ) -> list[DictItem]:
        """Every dictionary item, ordered by type, sort order and code."""
        where: list[str] = []
        params: list[Any] = []
        if dict_type:
            where.append("dict_type = ?")
            params.append(dict_type)
        if not include_disabled:
            where.append("enabled = 1")
        sql = "SELECT * FROM sys_dict_items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY dict_type ASC, sort_order ASC, code ASC"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict_item(row) for row in rows]

    def get_dicts_grouped(
        self, dict_type: str | None = None, include_disabled: bool = False
    ) -> dict[str, list[dict[str, Any]]]:
        """The same items, grouped by ``dict_type`` for the console payload."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in self.list_dicts(dict_type=dict_type, include_disabled=include_disabled):
            grouped.setdefault(item.dict_type, []).append(item.model_dump(mode="json"))
        return grouped

    def upsert_dict_item(self, item: DictItem) -> DictItem:
        """Insert or update one dictionary item by ``(dict_type, code)``."""
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sys_dict_items (
                    dict_type, code, label_zh, label_en, group_key, description,
                    sort_order, is_builtin, enabled, meta_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dict_type, code) DO UPDATE SET
                    label_zh = excluded.label_zh,
                    label_en = excluded.label_en,
                    group_key = excluded.group_key,
                    description = excluded.description,
                    sort_order = excluded.sort_order,
                    is_builtin = excluded.is_builtin,
                    enabled = excluded.enabled,
                    meta_json = excluded.meta_json,
                    updated_at = excluded.updated_at
                """,
                (
                    item.dict_type,
                    item.code,
                    item.label_zh,
                    item.label_en,
                    item.group_key,
                    item.description,
                    item.sort_order,
                    1 if item.is_builtin else 0,
                    1 if item.enabled else 0,
                    json.dumps(item.meta or {}, ensure_ascii=False),
                    item.created_at or now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM sys_dict_items WHERE dict_type = ? AND code = ?",
                (item.dict_type, item.code),
            ).fetchone()
            return self._row_to_dict_item(row)

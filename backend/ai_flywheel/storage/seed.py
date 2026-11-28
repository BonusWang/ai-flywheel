from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.storage.db import build_knowledge_search_text
from ai_flywheel.models import (
    CheckResult,
    CreateRuleRequest,
    NodeExecution,
    NodeStatus,
    OutcomeCheck,
    OutcomeOrigin,
    OutcomeStatus,
    PlannedWrite,
    PreflightRequest,
    RecordOutcomeRequest,
    ReportSignalRequest,
    RuleStatus,
    RunStatus,
    SessionRecord,
    SessionStatus,
    SignalKind,
    VerificationKind,
    WorkflowRunRecord,
)
from ai_flywheel.storage.db import FlywheelDB

if TYPE_CHECKING:
    from ai_flywheel.core.activation import ActivationEngine
    from ai_flywheel.core.receipts import ReceiptService


def seed_demo_data(
    cfg: FlywheelConfig,
    db: FlywheelDB,
    activation: ActivationEngine,
    receipts: ReceiptService,
) -> dict[str, int]:
    """Populate clean, replicable demo data when starting from scratch."""
    if db.list_rules() or db.list_workflow_runs():
        return {"seeded": 0}

    # 1. Built-in sample knowledge docs (never write demo files into an external mounted Wiki)
    if cfg.knowledge_sink.type == "builtin_kb":
        kb_root = cfg.knowledge_root_path
        lessons_dir = kb_root / "lessons"
        lessons_dir.mkdir(parents=True, exist_ok=True)
        sample_doc = lessons_dir / "2026-11-verify-before-claim.md"
        if not sample_doc.exists():
            sample_doc.write_text(
                "---\n"
                "title: 真实验证优先于静态判断\n"
                "tags: [verification, anti-laziness, mcp]\n"
                "---\n\n"
                "# 真实验证优先于静态判断\n\n"
                "## 背景与根因\n"
                "在多次脚本和 API 改动中，仅凭代码阅读判定“已完成”导致上线后仍存在路径或参数错误。\n\n"
                "## 沉淀规矩\n"
                "- 区分 `real`（真实运行）、`test`（单测）、`static`（静态阅读）与 `unknown`。\n"
                "- 凡声明 `applied_rule_ids` 的任务，必须附上真实命令输出或文件哈希证据。\n",
                encoding="utf-8",
            )
        sample_doc_2 = lessons_dir / "2026-11-retry-loop-breaker.md"
        if not sample_doc_2.exists():
            sample_doc_2.write_text(
                "---\n"
                "title: 同工具同因重试上限与换路原则\n"
                "tags: [debugging, retry-breaker, shell]\n"
                "---\n\n"
                "# 同工具同因重试上限与换路原则\n\n"
                "同一工具遇到相同报错原因最多重试 2 次；第 3 次必须停止盲目重试，改为检查解释器、环境路径或换专用工具。\n",
                encoding="utf-8",
            )
        seed_docs = [
            {
                "doc_path": "lessons/2026-11-verify-before-claim.md",
                "title": "真实验证优先于静态判断",
                "body": sample_doc.read_text(encoding="utf-8"),
                "tags": "verification anti-laziness mcp",
            },
            {
                "doc_path": "lessons/2026-11-retry-loop-breaker.md",
                "title": "同工具同因重试上限与换路原则",
                "body": sample_doc_2.read_text(encoding="utf-8"),
                "tags": "debugging retry-breaker shell",
            },
        ]
        for doc in seed_docs:
            doc["title_index"] = build_knowledge_search_text(doc["title"])
            doc["body_index"] = build_knowledge_search_text(doc["body"])
            doc["tags_index"] = build_knowledge_search_text(doc["tags"])
            doc["title_raw"] = doc["title"]
            doc["tags_raw"] = doc["tags"]
        db.rebuild_knowledge_fts(seed_docs)

    # 2. Sample sessions from Codex, Claude Code, and Claude Desktop
    now = datetime.now(timezone.utc)
    sessions = [
        SessionRecord(
            session_key="codex:demo-session-01",
            client="codex",
            session_id="demo-session-01",
            title="修复本地 MCP HTTP 端口绑定与并发锁测试",
            project="ai-flywheel",
            workspace="./",
            summary="排查 FastAPI 启动端口占用并补充独占锁回归测试，首次执行真实接口校验后通过。",
            content=(
                "User: 修复工作流并发锁并验证 MCP 接口。\n"
                "Assistant: 检查了 orchestrator 锁逻辑，发现异常路径未释锁。增加 fail-run 兜底并通过 pytest 与真实 curl 校验。"
            ),
            tags=["mcp", "workflow", "lock"],
            priority="P0_CORE",
            status=SessionStatus.PROCESSED,
            created_at=(now - timedelta(hours=18)).isoformat(),
            updated_at=(now - timedelta(hours=17)).isoformat(),
        ),
        SessionRecord(
            session_key="claude-code:demo-session-02",
            client="claude-code",
            session_id="demo-session-02",
            title="排查 PowerShell 引号转义导致的脚本失败",
            project="ai-flywheel",
            workspace="./",
            summary="在 Windows 下使用内联 python -c 触发单引号解析失败，连续报错 3 次触发死循环警告，最终改用落盘脚本解决。",
            content=(
                "User: 批量替换配置里的引号。\n"
                "Assistant: 连续 3 次尝试 PowerShell 内联 python -c 失败，触发 retry_loop 告警；换路落盘 .py 文件执行一次通过。"
            ),
            tags=["powershell", "debugging", "retry-breaker"],
            priority="P1_SIGNAL",
            status=SessionStatus.PENDING,
            created_at=(now - timedelta(hours=3)).isoformat(),
            updated_at=(now - timedelta(hours=2)).isoformat(),
        ),
        SessionRecord(
            session_key="claude:demo-session-03",
            client="claude",
            session_id="demo-session-03",
            title="梳理个人知识库与飞轮控制面解耦边界",
            project="architecture",
            workspace="",
            summary="通过 Claude Desktop MCP 推送摘要：明确策略留飞轮、领域知识留 Wiki、个人装备留 Home Repo。",
            content=(
                "User: 分析 Home Repo、Wiki 和 ai-flywheel 的边界。\n"
                "Assistant: 形成三体 SSOT 契约：策略与动态激活归飞轮，静态装备与私有 Profile 归 Home Repo，纯知识归 Wiki。"
            ),
            tags=["architecture", "knowledge", "mcp"],
            priority="P1_GENERAL",
            status=SessionStatus.PENDING,
            created_at=(now - timedelta(hours=1)).isoformat(),
            updated_at=(now - timedelta(minutes=40)).isoformat(),
        ),
    ]
    for s in sessions:
        db.upsert_session(s)

    # 3. Sample Rules covering all active lifecycle states
    r1 = activation.register_rule(
        CreateRuleRequest(
            rule_id="rule-real-verify-first",
            title="交付前必须区分真实运行验证与静态判断",
            domain="engineering",
            projects=["generic", "ai-flywheel"],
            triggers=["验证", "完成", "修复", "deploy", "test", "fix", "mcp"],
            checklist=[
                "区分 real / test / static / unknown 四类证据",
                "对涉及接口或脚本的改动执行至少一次真实调用",
            ],
            forbidden_routes=["只看静态代码未运行就声明已验证通过"],
            source_pointers=["lessons/2026-11-verify-before-claim.md"],
            origin_session_key="codex:demo-session-00",
        )
    )
    r2 = activation.register_rule(
        CreateRuleRequest(
            rule_id="rule-retry-max-2",
            title="同工具同报错最多重试 2 次，第 3 次必须换路",
            domain="engineering",
            projects=["generic", "ai-flywheel"],
            triggers=["重试", "报错", "失败", "powershell", "bash", "debug", "脚本"],
            checklist=[
                "第 2 次同因失败后检查解释器与环境路径",
                "复杂引号或批量文本处理直接落盘脚本执行",
            ],
            forbidden_routes=["无新证据连续第 3 次执行相同命令"],
            source_pointers=["lessons/2026-11-retry-loop-breaker.md"],
            origin_session_key="codex:demo-session-01",
        )
    )
    r3 = activation.register_rule(
        CreateRuleRequest(
            rule_id="rule-config-decouple-guard",
            title="公开产品仓库严禁硬编码个人绝对路径与私有业务域",
            domain="architecture",
            projects=["generic", "ai-flywheel"],
            triggers=["配置", "路径", "开源", "profile", "yaml", "架构"],
            checklist=[
                "个人偏好与业务触发词收口至外部 override_dir",
                "公开仓库仅保留脱敏默认模板",
            ],
            forbidden_routes=["把个人磁盘绝对路径写入默认代码或示例规则"],
            source_pointers=["profile/policies/anti_laziness.yaml"],
            origin_session_key="claude:demo-session-03",
        )
    )

    # 4. Run a real preflight + natural outcome to promote r1 to verified_active
    pf = activation.preflight_activate(
        PreflightRequest(
            task="修复 MCP 并发锁并做真实接口验证",
            project="ai-flywheel",
            client="codex",
            session_id="demo-session-01",
            turn_id="turn-1",
        )
    )
    receipts.record_outcome(
        RecordOutcomeRequest(
            session_key="codex:demo-session-01",
            client="codex",
            session_id="demo-session-01",
            turn_id="turn-1",
            preflight_receipt_id=pf.receipt_id,
            origin=OutcomeOrigin.NATURAL,
            status=OutcomeStatus.SUCCESS,
            summary="完成锁释放修复并通过真实接口 + 单元测试验证",
            checks=[
                OutcomeCheck(
                    name="真实接口与锁释放验证",
                    result=CheckResult.PASS,
                    kind=VerificationKind.REAL,
                    required=True,
                    evidence=["pytest tests/test_workflow.py -> 8 passed", "POST /api/site/runs -> 200 OK"],
                    rule_id=r1.rule_id,
                )
            ],
            applied_rule_ids=[r1.rule_id],
            artifacts=["lessons/2026-11-verify-before-claim.md"],
        )
    )

    # 5. Enqueue a realistic retro signal (marks r2 as needs_revision for demo clarity)
    receipts.report_signal(
        ReportSignalRequest(
            session_key="claude-code:demo-session-02",
            client="claude-code",
            session_id="demo-session-02",
            turn_id="turn-4",
            kind=SignalKind.RETRY_LOOP,
            severity="warning",
            summary="PowerShell 内联 python -c 引号解析连续失败 3 次，触发死循环保护告警",
            tool_name="run_command",
            rule_id=r2.rule_id,
            detail={"attempts": 3, "resolution": "switched to script file"},
        )
    )
    activation.transition_rule_status(
        r2.rule_id,
        RuleStatus.NEEDS_REVISION,
        note="示例：在 claude-code 会话中再次触发内联脚本重试告警，需补充 Windows 引号检查项",
    )

    # 6. Sample completed P00-P07 Workflow Run so visualizer shines immediately
    t0 = now - timedelta(hours=16)
    node_specs = [
        ("P00_PREPARE", "准备与对账", "prepare-worker", "策略校验通过，扫描 1 个新会话，P0 优先级排序完成"),
        ("P01_BEGIN", "独占启动", "lock-worker", "取得独占锁与准入预算（1/10 会话，18,420 chars）"),
        ("P02_REVIEW", "全文审查", "review-worker", "完成 1 个会话单元事实盘点，提取 2 项有效交付与 1 项验证证据"),
        ("P03_ROUTE_PLAN", "路由与写入计划", "merge-worker", "查重命中 1 条规则，规划 1 篇知识经验卡（Write-Set=1 ≤ 8）"),
        ("P04_WRITE_VERIFY", "受控写入与验收", "writer-worker", "写入 lessons/2026-11-verify-before-claim.md 并完成 SHA-256 回读核验"),
        ("P05_SETTLE", "台账与队列结算", "settlement-worker", "推进 rule-real-verify-first 晋升 verified_active，幂等回写会话队列"),
        ("P06_REPORT", "机器报告与摘要", "report-worker", "生成机器运行报告并刷新个人站统一读取层投影"),
        ("P07_DELIVER", "交付与终态释锁", "delivery-worker", "发布至本地个人站摘要流，校验 8 节点终态全部 PASS 后释放运行锁"),
    ]
    demo_nodes = []
    for idx, (nid, name, actor, summary) in enumerate(node_specs):
        st = (t0 + timedelta(seconds=idx * 4)).isoformat()
        ft = (t0 + timedelta(seconds=idx * 4 + 3)).isoformat()
        demo_nodes.append(
            NodeExecution(
                node_id=nid,
                name=name,
                actor=actor,
                status=NodeStatus.PASS,
                started_at=st,
                finished_at=ft,
                duration_ms=2800 + idx * 350,
                internal_checks=[
                    {"name": "合同与契约校验", "status": "PASS"},
                    {"name": "不可变回执落盘", "status": "PASS"},
                ],
                artifacts=["lessons/2026-11-verify-before-claim.md"] if nid == "P04_WRITE_VERIFY" else [],
                summary=summary,
                receipt_hash=f"demo{idx:02d}a7f9c12e4b",
            )
        )

    demo_run = WorkflowRunRecord(
        run_id="run-demo-001",
        trigger="seed",
        phase="daily",
        runner_mode="dry_run",
        status=RunStatus.PASS,
        admitted_sessions=["codex:demo-session-01"],
        nodes=demo_nodes,
        planned_writes=[
            PlannedWrite(
                target_kind="knowledge_doc",
                relative_path="lessons/2026-11-verify-before-claim.md",
                action="create",
                title="真实验证优先于静态判断",
                summary="沉淀真实验证分级规范与回执门禁要求",
                source_session_keys=["codex:demo-session-01"],
                approved=True,
            )
        ],
        digest_markdown=(
            "## AI Flywheel 运行日报（run-demo-001）\n\n"
            "- **流程状态**：PASS（8/8 节点通过，锁已释放）\n"
            "- **处理会话**：`codex:demo-session-01`\n"
            "- **规则演进**：`rule-real-verify-first` 由 `unverified_active` 自动晋升为 `verified_active`\n"
            "- **沉淀产物**：`lessons/2026-11-verify-before-claim.md`（回读校验 PASS）\n"
        ),
        lock_released=True,
        started_at=t0.isoformat(),
        finished_at=(t0 + timedelta(seconds=36)).isoformat(),
    )
    db.save_workflow_run(demo_run)
    return {"seeded": 1, "rules": 3, "sessions": 3, "runs": 1}

from __future__ import annotations

import json
from typing import Any

from ai_flywheel.config import FlywheelConfig


def _thin_hook_cmd(base_url: str, client: str) -> str:
    return f"flywheel hook-forward {client} --url {base_url}"


def build_client_connect_guides(cfg: FlywheelConfig) -> dict[str, Any]:
    """Generate ready-to-copy integration configs for Codex, Claude Code, and Claude Desktop."""
    base_url = f"http://{cfg.server.host}:{cfg.server.port}"
    mcp_http_url = f"{base_url}/mcp"

    codex_mcp_toml = (
        "[mcp_servers.ai_flywheel]\n"
        f'url = "{mcp_http_url}"\n'
    )
    codex_hooks_json = json.dumps(
        {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": _thin_hook_cmd(base_url, "codex")}]}
                ],
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": _thin_hook_cmd(base_url, "codex")}]}
                ],
                "SessionEnd": [
                    {"hooks": [{"type": "command", "command": _thin_hook_cmd(base_url, "codex")}]}
                ],
            }
        },
        ensure_ascii=False,
        indent=2,
    )

    claude_code_cli_cmd = f"claude mcp add --transport http ai-flywheel {mcp_http_url}"
    claude_code_hooks_json = json.dumps(
        {
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": _thin_hook_cmd(base_url, "claude-code")}]}
                ],
                "PreToolUse": [
                    {"hooks": [{"type": "command", "command": _thin_hook_cmd(base_url, "claude-code")}]}
                ],
                "Stop": [
                    {"hooks": [{"type": "command", "command": _thin_hook_cmd(base_url, "claude-code")}]}
                ],
            }
        },
        ensure_ascii=False,
        indent=2,
    )

    claude_desktop_json = json.dumps(
        {
            "mcpServers": {
                "ai-flywheel": {
                    "command": "flywheel",
                    "args": ["mcp-stdio"],
                }
            }
        },
        ensure_ascii=False,
        indent=2,
    )

    universal_skill_md = """# AI Flywheel Bridge Instructions (通用极薄规则)

在执行任何非平凡开发、排查、架构或文档沉淀任务时，按以下 3 步与 `ai-flywheel` MCP 协同：

1. **开局激活**：动手前调用 `flywheel_preflight_activate(task, project, client)`，严格遵守返回的反偷懒契约、个人协作偏好与命中规则的必查清单/禁用路线。
2. **异常上报**：若遇到同一工具同因连续失败、阻塞或规则复发，调用 `flywheel_report_signal` 入复盘队列。
3. **收尾闭环**：
   - 若命中规则或完成重要交付，调用 `flywheel_record_outcome` 提交真实证据（区分 `real/test/static/unknown`）；
   - 若当前客户端无本地日志文件（如 Claude Desktop），调用 `flywheel_ingest_session` 推送本轮摘要，交由后台 P00-P07 自主回顾与沉淀。
"""

    return {
        "endpoints": {
            "portal_url": base_url,
            "mcp_http_url": mcp_http_url,
            "mcp_stdio_cmd": "flywheel mcp-stdio",
            "llms_txt_url": f"{base_url}/llms.txt",
        },
        "clients": [
            {
                "id": "codex",
                "name": "Codex",
                "loop_guarantee": "100% 强制自动闭环（MCP + 极薄转发 Hook + P00 扫盘兜底）",
                "mcp_label": "config.toml MCP 配置",
                "mcp_config": codex_mcp_toml,
                "hook_label": "hooks.json 极薄转发 Hook（零业务逻辑）",
                "hook_config": codex_hooks_json,
                "notes": "Hook 只做 1 行 HTTP 转发，反偷懒与死循环规则全由 ai-flywheel 服务统一托管。",
            },
            {
                "id": "claude-code",
                "name": "Claude Code",
                "loop_guarantee": "100% 强制自动闭环（MCP + 极薄转发 Hook + P00 扫盘兜底）",
                "mcp_label": "一键添加 MCP 命令",
                "mcp_config": claude_code_cli_cmd,
                "hook_label": ".claude/settings.json 极薄转发 Hook",
                "hook_config": claude_code_hooks_json,
                "notes": "配合 ~/.claude/projects/**/*.jsonl 扫盘配置，即使异常退出也不会漏审会话。",
            },
            {
                "id": "claude",
                "name": "Claude (Desktop)",
                "loop_guarantee": "规则引导闭环（MCP stdio + 全局指令直推摘要）",
                "mcp_label": "claude_desktop_config.json",
                "mcp_config": claude_desktop_json,
                "hook_label": "粘贴到 Claude Preferences / Project Instructions",
                "hook_config": universal_skill_md,
                "notes": "Claude Desktop 无原生 Hook，通过指令引导收尾调用 flywheel_ingest_session 推入飞轮队列。",
            },
        ],
        "universal_skill_template": universal_skill_md,
    }

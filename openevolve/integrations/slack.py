"""
Slack integration for OpenEvolve.

Thin wrapper around slack-bolt in Socket Mode. Exposes:
- notify(text, channel=None): outbound message helper (for failures, run summaries, etc.)
- build_app(): returns a configured Bolt App with the /openevolve slash command wired up
- run(): convenience entrypoint used by scripts/slack_bot.py

Environment variables:
- SLACK_APP_TOKEN      xapp-... (connections:write, enables Socket Mode)
- SLACK_BOT_TOKEN      xoxb-... (chat:write, commands)
- SLACK_DEFAULT_CHANNEL optional; used by notify() when no channel passed
"""

from __future__ import annotations

import logging
import os
import shlex
from typing import Optional

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def format_run_result(
    program_id: str,
    metrics: dict,
    checkpoint_path: Optional[str] = None,
    usage_summary: Optional[dict] = None,
) -> str:
    """Format a run summary for Slack (Markdown-ish, Slack-flavored)."""
    lines = [f":white_check_mark: *OpenEvolve run complete* — best program `{program_id}`"]
    if metrics:
        lines.append("*Metrics:*")
        for k, v in metrics.items():
            if isinstance(v, (int, float)):
                lines.append(f"  • `{k}`: {v:.4f}")
            else:
                lines.append(f"  • `{k}`: {v}")
    if usage_summary and usage_summary.get("calls"):
        t = usage_summary["total"]
        lines.append(
            f"*Tokens:* {usage_summary['calls']} calls, "
            f"{t['input']} in / {t['output']} out / {t['total']} total"
        )
    if checkpoint_path:
        lines.append(f"*Checkpoint:* `{checkpoint_path}`")
    return "\n".join(lines)


def format_run_failure(error: str) -> str:
    return f":x: *OpenEvolve run failed*\n```{error}```"


def notify(text: str, channel: Optional[str] = None) -> None:
    """Send a one-off Slack message. No-op (with a log line) if tokens aren't configured."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    target = channel or os.environ.get("SLACK_DEFAULT_CHANNEL")
    if not token or not target:
        logger.debug("Slack notify skipped (missing SLACK_BOT_TOKEN or channel): %s", text)
        return
    try:
        from slack_sdk import WebClient
    except ImportError:
        logger.warning("slack-bolt not installed; install with: pip install -e '.[slack]'")
        return
    try:
        WebClient(token=token).chat_postMessage(channel=target, text=text)
    except Exception as e:
        logger.exception("Slack notify failed: %s", e)


# --- Slash command handlers -------------------------------------------------

def _handle_ping(args: list[str]) -> str:
    return "pong :wave:"


def _handle_stats(args: list[str]) -> str:
    return "stats: not yet wired — will read from the active checkpoint directory"


def _handle_tokens(args: list[str]) -> str:
    """Summarize token usage from the most recent run's usage.jsonl."""
    import glob

    from openevolve.llm.usage import summarize

    # Find the most recently modified usage.jsonl under any openevolve_output/ tree
    # rooted at CWD. Users typically run from an example dir, so CWD is the right base.
    candidates = glob.glob("**/openevolve_output/usage.jsonl", recursive=True)
    if not candidates:
        return "No `usage.jsonl` found under the current directory."
    path = max(candidates, key=lambda p: os.path.getmtime(p))
    s = summarize(path)
    if s["calls"] == 0:
        return f"`{path}` is empty — no calls recorded yet."
    lines = [f"*Token usage* (from `{path}`)", f"Total calls: {s['calls']}"]
    for (provider, model), b in sorted(s["per_model"].items()):
        lines.append(
            f"• `{provider}/{model}` — {b['calls']} calls, "
            f"{b['input']} in / {b['output']} out / {b['total']} total"
        )
    t = s["total"]
    lines.append(f"*Grand total:* {t['input']} in / {t['output']} out / {t['total']} total")
    return "\n".join(lines)


def _handle_rerun(args: list[str]) -> str:
    return "rerun: not yet wired — will re-launch the last experiment"


_SUBCOMMANDS = {
    "ping": _handle_ping,
    "stats": _handle_stats,
    "tokens": _handle_tokens,
    "rerun": _handle_rerun,
}


def _dispatch(text: str) -> str:
    parts = shlex.split(text or "")
    if not parts:
        return (
            "Usage: `/openevolve <subcommand>`\n"
            "Subcommands: " + ", ".join(sorted(_SUBCOMMANDS)) + "\n"
            "Try `/openevolve ping`."
        )
    name, *rest = parts
    handler = _SUBCOMMANDS.get(name)
    if not handler:
        return f"Unknown subcommand `{name}`. Known: {', '.join(sorted(_SUBCOMMANDS))}"
    try:
        return handler(rest)
    except Exception as e:
        logger.exception("Subcommand %s failed", name)
        return f":warning: `{name}` raised: {e}"


def build_app():
    """Build a Bolt App with /openevolve wired up. Caller provides tokens via env."""
    from slack_bolt import App

    app = App(token=_require("SLACK_BOT_TOKEN"))

    @app.command("/openevolve")
    def _on_command(ack, respond, command):
        ack()
        respond(_dispatch(command.get("text", "")))

    return app


def run() -> None:
    """Start the Socket Mode listener. Blocks forever."""
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    logging.basicConfig(level=logging.INFO)
    app = build_app()
    handler = SocketModeHandler(app, _require("SLACK_APP_TOKEN"))
    logger.info("Starting OpenEvolve Slack bot (Socket Mode)...")
    handler.start()

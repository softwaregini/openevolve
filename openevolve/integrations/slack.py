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
import re
import shlex
from typing import Optional

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def format_run_start(
    run_id: str,
    output_dir: str,
    models: Optional[list] = None,
    iterations: Optional[int] = None,
    experiment: Optional[str] = None,
) -> str:
    """Format a 'run started' message for Slack."""
    header = ":rocket: *OpenEvolve run started*"
    if experiment:
        header += f" — `{experiment}`"
    lines = [header, f"*Run id:* `{run_id}`"]
    if models:
        lines.append(f"*Models:* {', '.join(models)}")
    if iterations is not None:
        lines.append(f"*Iterations:* {iterations}")
    lines.append(f"*Output dir:* `{output_dir}`")
    return "\n".join(lines)


def _format_delta(old: float, new: float) -> str:
    delta = new - old
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.4f}"


def format_run_result(
    program_id: str,
    metrics: dict,
    checkpoint_path: Optional[str] = None,
    usage_summary: Optional[dict] = None,
    run_id: Optional[str] = None,
    experiment: Optional[str] = None,
    initial_metrics: Optional[dict] = None,
) -> str:
    """Format a run summary for Slack (Markdown-ish, Slack-flavored)."""
    header = ":white_check_mark: *OpenEvolve run complete*"
    if experiment:
        header += f" — `{experiment}`"
    lines = [header]
    if run_id:
        lines.append(f"*Run id:* `{run_id}`")
    lines.append(f"*Best program:* `{program_id}`")
    if metrics:
        lines.append("*Metrics (best → vs initial):*" if initial_metrics else "*Metrics:*")
        for k, v in metrics.items():
            if isinstance(v, (int, float)):
                if initial_metrics and isinstance(initial_metrics.get(k), (int, float)):
                    delta = _format_delta(initial_metrics[k], v)
                    lines.append(
                        f"  • `{k}`: {v:.4f}  (initial {initial_metrics[k]:.4f} · Δ {delta})"
                    )
                else:
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


def format_run_failure(
    error: str,
    run_id: Optional[str] = None,
    log_dir: Optional[str] = None,
    iterations_completed: Optional[int] = None,
    best_metrics: Optional[dict] = None,
    usage_summary: Optional[dict] = None,
    experiment: Optional[str] = None,
) -> str:
    header = ":x: *OpenEvolve run failed*"
    if experiment:
        header += f" — `{experiment}`"
    lines = [header]
    if run_id:
        lines.append(f"*Run id:* `{run_id}`")
    lines.append(f"```{error}```")
    if iterations_completed is not None:
        lines.append(f"*Iterations completed:* {iterations_completed}")
    if best_metrics:
        lines.append("*Best metrics so far:*")
        for k, v in best_metrics.items():
            if isinstance(v, (int, float)):
                lines.append(f"  • `{k}`: {v:.4f}")
            else:
                lines.append(f"  • `{k}`: {v}")
    if usage_summary and usage_summary.get("calls"):
        t = usage_summary["total"]
        lines.append(
            f"*Tokens spent:* {usage_summary['calls']} calls, {t['total']} total"
        )
    if log_dir:
        lines.append(f"*Logs:* `{log_dir}`")
    lines.append(f"_Rerun with `/openevolve rerun`_" if run_id else "")
    return "\n".join(l for l in lines if l)


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


def _handle_tokens(args: list[str]) -> str:
    """Summarize token usage from usage.jsonl.

    Usage: `/openevolve tokens` (latest run) | `tokens all` | `tokens <run_id>`
    """
    import glob

    from openevolve.llm.usage import summarize

    candidates = glob.glob("**/openevolve_output/usage.jsonl", recursive=True)
    if not candidates:
        return "No `usage.jsonl` found under the current directory."
    path = max(candidates, key=lambda p: os.path.getmtime(p))

    arg = (args[0] if args else "latest").strip()
    run_id_filter: Optional[str]
    scope: str
    if arg == "all":
        run_id_filter = None
        scope = "all runs"
    elif arg == "latest":
        run_id_filter = "latest"
        scope = "latest run"
    else:
        run_id_filter = arg
        scope = f"run `{arg}`"

    s = summarize(path, run_id=run_id_filter)
    if s["calls"] == 0:
        return f"No calls recorded for {scope} in `{path}`."
    header = f"*Token usage* — {scope}"
    if s.get("run_id"):
        header += f" (`{s['run_id']}`)"
    lines = [header, f"Total calls: {s['calls']}"]
    for (provider, model), b in sorted(s["per_model"].items()):
        lines.append(
            f"• `{provider}/{model}` — {b['calls']} calls, "
            f"{b['input']} in / {b['output']} out / {b['total']} total"
        )
    t = s["total"]
    lines.append(f"*Grand total:* {t['input']} in / {t['output']} out / {t['total']} total")
    return "\n".join(lines)


def _handle_rerun(args: list[str]) -> str:
    """Re-launch the most recently started run using its manifest.

    Usage:
      /openevolve rerun              -> relaunch if previous run has ended
      /openevolve rerun force        -> relaunch even if previous is still running
    """
    import subprocess

    from openevolve.llm.usage import get_log_path
    from openevolve.run_manifest import load_last_run

    manifest = load_last_run()
    if not manifest:
        return (
            "No previous run found. Start one locally first; rerun reads "
            "`~/.openevolve/last_run.json`."
        )

    force = bool(args and args[0] == "force")
    if not force and _run_still_active(manifest):
        return (
            f":warning: Previous run `{manifest.get('run_id')}` has no `run_end` "
            "marker — it may still be active. Use `/openevolve rerun force` to launch anyway."
        )

    argv = manifest.get("argv") or []
    cwd = manifest.get("cwd")
    if not argv or not cwd:
        return f":x: Manifest is incomplete: {manifest}"

    try:
        # Detached: don't hold the bot while the run executes.
        subprocess.Popen(
            argv,
            cwd=cwd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.exception("Failed to spawn rerun")
        return f":x: Could not spawn rerun: {e}"

    return (
        f":arrows_counterclockwise: Rerun launched in `{cwd}`\n"
        f"Previous run: `{manifest.get('run_id')}` — "
        "a new `run_started` message will follow once the controller initializes."
    )


def _run_still_active(manifest: dict) -> bool:
    """True if the previous run's usage.jsonl has a run_start without a matching run_end."""
    import json as _json

    out = manifest.get("output_dir")
    run_id = manifest.get("run_id")
    if not out or not run_id:
        return False
    log_path = os.path.join(out, "usage.jsonl")
    if not os.path.exists(log_path):
        return False
    saw_start = False
    saw_end = False
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if row.get("run_id") != run_id:
                    continue
                if row.get("event") == "run_start":
                    saw_start = True
                elif row.get("event") == "run_end":
                    saw_end = True
    except OSError:
        return False
    return saw_start and not saw_end


_EXPERIMENTS_DIR = "experiments"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _experiments_root() -> str:
    """Resolve experiments/ relative to the repo (parent of this package)."""
    here = os.path.dirname(os.path.abspath(__file__))
    # openevolve/integrations/slack.py -> repo root is two levels up
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    return os.path.join(repo_root, _EXPERIMENTS_DIR)


def _handle_list(args: list[str]) -> str:
    """List experiments under ./experiments/."""
    root = _experiments_root()
    if not os.path.isdir(root):
        return (
            f"No `{_EXPERIMENTS_DIR}/` directory yet. Create one at the repo root and "
            "drop experiment subdirs into it."
        )
    names = sorted(
        d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
    )
    if not names:
        return f"`{_EXPERIMENTS_DIR}/` is empty."
    return "*Experiments:*\n" + "\n".join(f"• `{n}`" for n in names)


def _handle_run(args: list[str]) -> str:
    """Launch an experiment by name: /openevolve run <name>."""
    import subprocess
    import sys

    if not args:
        return "Usage: `/openevolve run <name>`. Use `/openevolve list` to see available names."
    name = args[0]
    if not _NAME_RE.match(name):
        return (
            f":x: Invalid experiment name `{name}`. "
            "Allowed: letters, digits, underscore, hyphen."
        )
    exp_dir = os.path.join(_experiments_root(), name)
    if not os.path.isdir(exp_dir):
        return f":x: No experiment `{name}` under `{_EXPERIMENTS_DIR}/`."
    initial = os.path.join(exp_dir, "initial_program.py")
    evaluator = os.path.join(exp_dir, "evaluator.py")
    config = os.path.join(exp_dir, "config.yaml")
    missing = [p for p in (initial, evaluator, config) if not os.path.isfile(p)]
    if missing:
        return f":x: Experiment `{name}` is missing: {', '.join(os.path.basename(p) for p in missing)}"

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    argv = [
        sys.executable,
        os.path.join(repo_root, "openevolve-run.py"),
        initial,
        evaluator,
        "--config",
        config,
    ]
    try:
        subprocess.Popen(
            argv,
            cwd=exp_dir,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.exception("Failed to launch experiment %s", name)
        return f":x: Could not launch `{name}`: {e}"
    return (
        f":rocket: Launched experiment `{name}` in `{exp_dir}`\n"
        "A `run_started` Slack message will follow once the controller initializes."
    )


def _handle_help(args: list[str]) -> str:
    """Show available subcommands."""
    lines = ["*OpenEvolve Slack commands*"]
    # Keep ordering logical (discovery → action → monitoring), not alphabetical.
    for name in ("help", "ping", "list", "run", "rerun", "tokens"):
        desc = _SUBCOMMAND_HELP.get(name, "")
        lines.append(f"• `/openevolve {name}` — {desc}")
    return "\n".join(lines)


_SUBCOMMAND_HELP = {
    "help": "show this message",
    "ping": "health check (bot is alive)",
    "list": "list experiments under `experiments/`",
    "run": "launch an experiment: `run <name>`",
    "rerun": "re-launch the last run (`rerun force` to override the active-run guard)",
    "tokens": "token usage: `tokens` (latest) | `tokens all` | `tokens <run_id>`",
}


_SUBCOMMANDS = {
    "help": _handle_help,
    "ping": _handle_ping,
    "tokens": _handle_tokens,
    "rerun": _handle_rerun,
    "list": _handle_list,
    "run": _handle_run,
}


def _clean_arg(s: str) -> str:
    """Strip Slack-added decorators (backticks, bold/italic markers, whitespace)."""
    return s.strip().strip("`*_ ").strip()


def _dispatch(text: str) -> str:
    parts = [_clean_arg(p) for p in shlex.split(text or "")]
    parts = [p for p in parts if p]
    if not parts:
        return _handle_help([])
    name, *rest = parts
    handler = _SUBCOMMANDS.get(name)
    if not handler:
        return f":question: Unknown subcommand `{name}`.\n\n" + _handle_help([])
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

"""
Run manifest — durable record of how a run was launched.

Written at the start of every CLI run into two places:
  1. <output_dir>/run_manifest.json — self-contained per-run record.
  2. ~/.openevolve/last_run.json — pointer so the Slack bot (or any tool)
     can find the most recent run without knowing the working directory.

Contents are intentionally minimal: enough to reproduce the invocation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _global_state_path() -> Path:
    return Path.home() / ".openevolve" / "last_run.json"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_manifest(
    output_dir: str,
    run_id: str,
    argv: list,
    cwd: str,
    config_path: Optional[str] = None,
) -> str:
    """Write the per-run manifest and update the global last-run pointer.

    Returns the absolute path to the per-run manifest.
    """
    manifest = {
        "run_id": run_id,
        "experiment": os.path.basename(os.path.abspath(cwd)),
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
        "argv": list(argv),
        "cwd": os.path.abspath(cwd),
        "output_dir": os.path.abspath(output_dir),
        "config_path": os.path.abspath(config_path) if config_path else None,
    }
    manifest_path = Path(output_dir) / "run_manifest.json"
    _atomic_write(manifest_path, manifest)
    _atomic_write(_global_state_path(), {"manifest_path": str(manifest_path.resolve())})
    return str(manifest_path.resolve())


def load_last_run() -> Optional[dict]:
    """Load the most recent run's manifest. Returns None if no run has been recorded."""
    pointer_path = _global_state_path()
    if not pointer_path.exists():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        manifest_path = Path(pointer["manifest_path"])
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError, OSError):
        return None

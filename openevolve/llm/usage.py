"""
Durable token-usage logging.

Each LLM call appends a single JSON line to a usage log file and fsyncs before
returning. POSIX guarantees O_APPEND atomicity for writes smaller than PIPE_BUF
(typically 4KB) — our lines are well under 200 bytes — so multiple worker
processes can safely append to the same file without locking.

The log path is read from the OPENEVOLVE_USAGE_LOG environment variable, which
the controller sets once per run. Worker subprocesses inherit it through the
ProcessPoolExecutor `spawn` start method.

Line schemas (one JSON object per line; discriminated by presence of `event`):

  Usage record (LLM call):
    {
        "ts": "2026-04-23T10:15:30.123456+00:00",
        "run_id": "<uuid, if OPENEVOLVE_RUN_ID is set>",
        "provider": "bedrock" | "openai" | ...,
        "model": "<model id>",
        "input_tokens": int,
        "output_tokens": int,
        "total_tokens": int
    }

  Event marker (run boundary):
    {
        "ts": "...",
        "event": "run_start" | "run_end",
        "run_id": "<uuid>",
        ... additional keys per event ...
    }

Consumers can either group records by `run_id`, or split on `event` markers.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_VAR = "OPENEVOLVE_USAGE_LOG"
_RUN_ID_ENV = "OPENEVOLVE_RUN_ID"


def get_log_path() -> Optional[str]:
    return os.environ.get(_ENV_VAR) or None


def _append_line(obj: dict) -> None:
    path = get_log_path()
    if not path:
        return
    line = json.dumps(obj, ensure_ascii=False)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception as e:
        logger.warning("Usage log write failed: %s", e)


def write_event(event: str, **fields) -> None:
    """Append a JSON marker line (e.g. run_start, run_end). Silent no-op without env."""
    obj = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "event": event,
        "run_id": os.environ.get(_RUN_ID_ENV),
    }
    obj.update(fields)
    _append_line(obj)


def record(
    provider: str,
    model: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    total_tokens: Optional[int] = None,
) -> None:
    """Append a usage record. Silent no-op if the env var is unset."""
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    _append_line(
        {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "run_id": os.environ.get(_RUN_ID_ENV),
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    )


def summarize(path: Optional[str] = None, run_id: Optional[str] = None) -> dict:
    """Aggregate a usage log into totals per (provider, model).

    Skips event marker lines. If `run_id` is given, only records with that
    run_id are counted (pass "latest" to use the last run_start marker).
    """
    path = path or get_log_path()
    if not path or not os.path.exists(path):
        return {"calls": 0, "per_model": {}, "total": {"input": 0, "output": 0, "total": 0}}

    if run_id == "latest":
        run_id = _find_latest_run_id(path)

    per_model: dict = {}
    totals = {"input": 0, "output": 0, "total": 0}
    calls = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event" in row:
                continue
            if run_id is not None and row.get("run_id") != run_id:
                continue
            calls += 1
            key = (row.get("provider") or "?", row.get("model") or "?")
            bucket = per_model.setdefault(
                key, {"calls": 0, "input": 0, "output": 0, "total": 0}
            )
            bucket["calls"] += 1
            for src, dst in (
                ("input_tokens", "input"),
                ("output_tokens", "output"),
                ("total_tokens", "total"),
            ):
                v = row.get(src)
                if isinstance(v, int):
                    bucket[dst] += v
                    totals[dst] += v
    return {"calls": calls, "per_model": per_model, "total": totals, "run_id": run_id}


def _find_latest_run_id(path: str) -> Optional[str]:
    latest = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") == "run_start" and row.get("run_id"):
                latest = row["run_id"]
    return latest

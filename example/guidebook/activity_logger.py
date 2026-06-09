# activity_logger.py
"""
Structured JSON logging for MCP tool calls and user activity.

Writes newline-delimited JSON (NDJSON) to a shared log file.
The dashboard sidecar reads this same file for analytics.

Features:
- Buffered writes (flush every N records or M seconds)
- Automatic log rotation with gzip compression
- Thread-safe for multi-threaded gunicorn workers
"""
from __future__ import annotations

import atexit
import gzip
import json
import logging
import os
import shutil
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------
# Configuration
# ---------------------
LOG_DIR = Path(os.getenv("MCP_LOG_DIR", "/var/log/mcp"))
TOOL_LOG_FILE = LOG_DIR / "tool_calls.jsonl"
ACTIVITY_LOG_FILE = LOG_DIR / "activities.jsonl"

ROTATE_SIZE_BYTES = int(os.getenv("MCP_LOG_ROTATE_SIZE_BYTES", "5242880"))  # 5 MB
BUFFER_MAX_RECORDS = int(os.getenv("MCP_LOG_BUFFER_MAX", "50"))
BUFFER_FLUSH_INTERVAL_SECS = float(os.getenv("MCP_LOG_FLUSH_INTERVAL", "5.0"))

_write_lock = threading.Lock()
_buffers: Dict[str, list] = defaultdict(list)
_flush_timer: Optional[threading.Timer] = None


def _ensure_log_dir():
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _maybe_rotate(filepath: Path) -> None:
    if not filepath.exists():
        return
    try:
        size = filepath.stat().st_size
    except OSError:
        return
    if size < ROTATE_SIZE_BYTES:
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    stem = filepath.stem
    rotated = filepath.parent / f"{stem}.{ts}.jsonl.gz"

    with open(filepath, "rb") as f_in:
        with gzip.open(rotated, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

    with open(filepath, "w") as f:
        pass


def _flush_buffer_locked(filepath: Path) -> None:
    key = str(filepath)
    lines = _buffers.get(key)
    if not lines:
        return
    _maybe_rotate(filepath)
    with open(filepath, "a", encoding="utf-8") as f:
        f.writelines(lines)
    _buffers[key] = []


def _flush_all() -> None:
    with _write_lock:
        for key in list(_buffers.keys()):
            if _buffers[key]:
                filepath = Path(key)
                _maybe_rotate(filepath)
                with open(filepath, "a", encoding="utf-8") as f:
                    f.writelines(_buffers[key])
                _buffers[key] = []


def _ensure_flush_timer() -> None:
    global _flush_timer
    if _flush_timer is not None and _flush_timer.is_alive():
        return
    _flush_timer = threading.Timer(BUFFER_FLUSH_INTERVAL_SECS, _flush_all)
    _flush_timer.daemon = True
    _flush_timer.start()


atexit.register(_flush_all)


def _append_jsonl(filepath: Path, record: Dict[str, Any]) -> None:
    _ensure_log_dir()
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with _write_lock:
        key = str(filepath)
        _buffers[key].append(line)
        if len(_buffers[key]) >= BUFFER_MAX_RECORDS:
            _flush_buffer_locked(filepath)
        else:
            _ensure_flush_timer()


def log_tool_call(
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    response_ok: bool,
    latency_ms: float,
    input_tokens_est: int = 0,
    output_tokens_est: int = 0,
    session_id: Optional[str] = None,
) -> None:
    record = {
        "type": "tool_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "arguments": arguments,
        "response_ok": response_ok,
        "latency_ms": round(latency_ms, 3),
        "input_tokens_est": input_tokens_est,
        "output_tokens_est": output_tokens_est,
    }
    if session_id:
        record["session_id"] = session_id
    _append_jsonl(TOOL_LOG_FILE, record)


def log_activity(
    *,
    user_goal: str = "",
    artifact_type: str = "",
    artifact_summary: str = "",
    grade_level: str = "",
    subject_area: str = "",
    tools_used: Optional[list] = None,
    session_id: Optional[str] = None,
) -> None:
    record = {
        "type": "activity",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_goal": user_goal,
        "artifact_type": artifact_type,
        "artifact_summary": artifact_summary,
        "grade_level": grade_level,
        "subject_area": subject_area,
    }
    if tools_used:
        record["tools_used"] = tools_used
    if session_id:
        record["session_id"] = session_id
    _append_jsonl(ACTIVITY_LOG_FILE, record)


def setup_logging():
    logger = logging.getLogger("cps_guidebook_mcp")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


_logger = setup_logging()


def log_info(msg: str, **kwargs):
    record = {
        "level": "info",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": msg,
        **kwargs,
    }
    _logger.info(json.dumps(record, default=str))


def log_error(msg: str, **kwargs):
    record = {
        "level": "error",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": msg,
        **kwargs,
    }
    _logger.error(json.dumps(record, default=str))

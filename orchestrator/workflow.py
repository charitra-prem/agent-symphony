"""Symphony-style WORKFLOW.md loader with hot-reload.

Single source of truth for orchestrator tuning. Reads /etc/triage/workflow.md,
parses the YAML front matter, and re-applies on file change.

Invalid YAML on reload keeps the last-known-good in memory and logs a warning.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver as Observer

log = logging.getLogger("workflow")

WORKFLOW_PATH = Path(os.environ.get("WORKFLOW_PATH", "/etc/triage/workflow.md"))

DEFAULTS: dict[str, Any] = {
    "tracker": {
        "kind": "linear",
        "endpoint": "https://api.linear.app/graphql",
        "api_key": "$LINEAR_API_KEY",
        "required_labels": [],
        "active_states": ["Backlog", "Todo", "In Progress"],
        "terminal_states": ["Done", "Cancelled", "Canceled", "Duplicate", "Closed"],
        "block_on_open_blockers": True,
    },
    "polling": {"interval_ms": 60_000},
    "workspace": {"root": "/root/work/repos"},
    "agent": {
        "max_concurrent_agents": 8,
        "max_concurrent_agents_by_state": {},
        "max_retry_backoff_ms": 300_000,
        "max_turns": 20,
        "stall_timeout_ms": 600_000,
    },
    "overrides_enabled": True,
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_env_refs(d: Any) -> Any:
    """Substitute $VAR style references using os.environ."""
    if isinstance(d, dict):
        return {k: _resolve_env_refs(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_resolve_env_refs(v) for v in d]
    if isinstance(d, str) and d.startswith("$"):
        return os.environ.get(d[1:], d)
    return d


def _parse_workflow_md(text: str) -> dict[str, Any]:
    """Extract YAML front matter from a markdown file.

    Format:
        ---
        yaml here
        ---
        markdown body (ignored)
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("workflow.md must begin with --- front matter")
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError("workflow.md front matter not closed (no second ---)")
    yaml_text = "\n".join(lines[1:end])
    parsed = yaml.safe_load(yaml_text) or {}
    if not isinstance(parsed, dict):
        raise ValueError("workflow.md front matter must be a mapping")
    return parsed


# ---- live config singleton (thread-safe read-mostly) ----
_config: dict[str, Any] = deepcopy(DEFAULTS)
_config_lock = threading.RLock()
_last_loaded_at: float = 0.0
_last_mtime: float = 0.0
_observer: Observer | None = None


def get() -> dict[str, Any]:
    """Return a shallow copy of the current config. Cheap; safe to call in hot paths."""
    with _config_lock:
        return _config


def reload_now() -> bool:
    """Re-read WORKFLOW_PATH. Returns True on successful update."""
    global _config, _last_loaded_at, _last_mtime
    try:
        if not WORKFLOW_PATH.exists():
            log.warning("workflow path missing: %s; keeping last-known-good", WORKFLOW_PATH)
            return False
        st = WORKFLOW_PATH.stat()
        text = WORKFLOW_PATH.read_text()
        parsed = _parse_workflow_md(text)
        merged = _deep_merge(DEFAULTS, parsed)
        merged = _resolve_env_refs(merged)
        with _config_lock:
            _config = merged
            _last_loaded_at = time.time()
            _last_mtime = st.st_mtime
        log.info("workflow.md reloaded: mtime=%s keys=%s", st.st_mtime, list(merged.keys()))
        return True
    except Exception as exc:
        log.warning("workflow.md reload FAILED, keeping last-known-good: %s", exc)
        return False


class _Handler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.is_directory:
            return
        if Path(event.src_path).resolve() == WORKFLOW_PATH.resolve():
            reload_now()

    def on_created(self, event):
        if event.is_directory:
            return
        if Path(event.src_path).resolve() == WORKFLOW_PATH.resolve():
            reload_now()


def start_watcher() -> None:
    """Begin watching WORKFLOW_PATH for changes. Idempotent."""
    global _observer
    if _observer is not None:
        return
    reload_now()
    obs = Observer()
    obs.schedule(_Handler(), path=str(WORKFLOW_PATH.parent), recursive=False)
    obs.daemon = True
    obs.start()
    _observer = obs
    log.info("workflow watcher started on %s", WORKFLOW_PATH.parent)


def stop_watcher() -> None:
    global _observer
    if _observer is not None:
        _observer.stop()
        _observer.join(timeout=2)
        _observer = None


# ---- convenience accessors ----
def tracker() -> dict[str, Any]:
    return get()["tracker"]


def polling_interval_s() -> float:
    return get()["polling"]["interval_ms"] / 1000.0


def max_concurrent() -> int:
    return get()["agent"]["max_concurrent_agents"]


def max_concurrent_for_state(state: str) -> int:
    cfg = get()["agent"]
    overrides = cfg.get("max_concurrent_agents_by_state") or {}
    return int(overrides.get(state, cfg["max_concurrent_agents"]))


def stall_timeout_ms() -> int:
    return int(get()["agent"]["stall_timeout_ms"])


def max_retry_backoff_ms() -> int:
    return int(get()["agent"]["max_retry_backoff_ms"])


def required_labels() -> list[str]:
    return list(tracker().get("required_labels") or [])


def active_states() -> set[str]:
    return set(tracker().get("active_states") or [])


def terminal_states() -> set[str]:
    return set(tracker().get("terminal_states") or [])


def block_on_open_blockers() -> bool:
    return bool(tracker().get("block_on_open_blockers", True))


SECRET_KEY_FRAGMENTS = ("key", "secret", "token", "password")


def _redact(d):
    if isinstance(d, dict):
        return {k: ("<redacted>" if any(f in k.lower() for f in SECRET_KEY_FRAGMENTS) and isinstance(v, str) else _redact(v)) for k, v in d.items()}
    if isinstance(d, list):
        return [_redact(v) for v in d]
    return d


def status_snapshot() -> dict[str, Any]:
    """For the /api/v1/state debug endpoint. Secrets redacted."""
    with _config_lock:
        return {
            "path": str(WORKFLOW_PATH),
            "last_loaded_at": _last_loaded_at,
            "last_mtime": _last_mtime,
            "config": _redact(_config),
        }

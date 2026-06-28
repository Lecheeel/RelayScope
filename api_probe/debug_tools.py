from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DEBUG_LOCK = threading.Lock()
_REDACT_KEYS = {
    "api_key",
    "authorization",
    "x-api-key",
    "bearer",
    "token",
    "secret",
    "session_id",
    "agent_id",
    "prompt_cache_key",
}


def is_debug_enabled(config: Any) -> bool:
    metadata = getattr(config, "metadata", None)
    if isinstance(metadata, dict):
        return bool(metadata.get("debug_mode"))
    return bool(getattr(config, "debug_mode", False))


def debug_log_path(config: Any) -> Path:
    metadata = getattr(config, "metadata", None)
    if isinstance(metadata, dict):
        path = metadata.get("debug_log_path")
        if isinstance(path, str) and path.strip():
            return Path(path.strip())
    return Path("api_probe_debug.log")


def log_debug_event(config: Any, event: str, data: dict[str, Any] | None = None) -> None:
    if not is_debug_enabled(config):
        return
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "target": _target_snapshot(config),
        "data": _sanitize(data or {}),
    }
    path = debug_log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _DEBUG_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _target_snapshot(config: Any) -> dict[str, Any]:
    profile = getattr(getattr(config, "client_profile", None), "value", None)
    provider = getattr(getattr(config, "provider_family", None), "value", None)
    protocol = getattr(getattr(config, "protocol_mode", None), "value", None)
    return {
        "name": getattr(config, "name", None),
        "provider": provider,
        "protocol": protocol,
        "profile": profile,
        "model": getattr(config, "model", None),
        "base_url": getattr(config, "base_url", None),
    }


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _REDACT_KEYS or lowered.endswith("authorization"):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item) for item in value)
    if isinstance(value, str):
        if len(value) > 2000:
            return value[:2000] + "…"
        return value
    return value

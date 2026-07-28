"""Advisory soft locks for override edit sections (does not replace optimistic rev)."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .override_store import OVERRIDE_DIR, SECTIONS, _safe_name
from .timeutil import now_beijing_iso

LOCK_TTL_SECONDS = 600
LOCKS_PATH = OVERRIDE_DIR / ".section_locks.json"

_lock = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_expired(entry: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or _utc_now()
    exp = _parse_iso(entry.get("expiresAt"))
    if exp is None:
        return True
    return exp <= now


def _load_all() -> dict[str, Any]:
    if not LOCKS_PATH.exists():
        return {}
    try:
        data = json.loads(LOCKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: dict[str, Any]) -> None:
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOCKS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(LOCKS_PATH)


def _purge_expired(sprint_map: dict[str, Any], now: datetime) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for section, entry in sprint_map.items():
        if section not in SECTIONS:
            continue
        if not isinstance(entry, dict):
            continue
        if _is_expired(entry, now):
            continue
        cleaned[section] = entry
    return cleaned


def _public_lock(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if not entry:
        return None
    return {
        "section": entry.get("section"),
        "editor": entry.get("editor") or "匿名",
        "token": entry.get("token"),
        "expiresAt": entry.get("expiresAt"),
        "updatedAt": entry.get("updatedAt"),
    }


def get_locks(sprint: str) -> dict[str, Any | None]:
    key = _safe_name(sprint)
    now = _utc_now()
    with _lock:
        all_data = _load_all()
        sprint_map = all_data.get(key) if isinstance(all_data.get(key), dict) else {}
        cleaned = _purge_expired(sprint_map, now)
        if cleaned != sprint_map:
            if cleaned:
                all_data[key] = cleaned
            else:
                all_data.pop(key, None)
            _save_all(all_data)
        return {s: _public_lock(cleaned.get(s)) for s in SECTIONS}


def acquire_lock(
    sprint: str,
    *,
    section: str,
    editor: str,
    token: str | None = None,
) -> dict[str, Any]:
    if section not in SECTIONS:
        raise ValueError(f"未知分区: {section}")
    editor_name = (editor or "").strip() or "匿名"
    key = _safe_name(sprint)
    now = _utc_now()
    expires = now + timedelta(seconds=LOCK_TTL_SECONDS)
    with _lock:
        all_data = _load_all()
        sprint_map = all_data.get(key) if isinstance(all_data.get(key), dict) else {}
        sprint_map = _purge_expired(sprint_map, now)
        current = sprint_map.get(section)
        use_token = (token or "").strip() or str(uuid.uuid4())
        if (
            isinstance(current, dict)
            and not _is_expired(current, now)
            and current.get("token")
            and current.get("token") != use_token
        ):
            return {
                "acquired": False,
                "section": section,
                "lock": _public_lock(current),
                "locks": {s: _public_lock(sprint_map.get(s)) for s in SECTIONS},
            }
        entry = {
            "section": section,
            "editor": editor_name,
            "token": use_token,
            "updatedAt": now_beijing_iso(),
            "expiresAt": expires.isoformat().replace("+00:00", "Z"),
        }
        sprint_map[section] = entry
        all_data[key] = sprint_map
        _save_all(all_data)
        return {
            "acquired": True,
            "section": section,
            "lock": _public_lock(entry),
            "locks": {s: _public_lock(sprint_map.get(s)) for s in SECTIONS},
        }


def heartbeat_lock(
    sprint: str,
    *,
    section: str,
    token: str,
) -> dict[str, Any]:
    if section not in SECTIONS:
        raise ValueError(f"未知分区: {section}")
    token = (token or "").strip()
    if not token:
        raise ValueError("token 不能为空")
    key = _safe_name(sprint)
    now = _utc_now()
    expires = now + timedelta(seconds=LOCK_TTL_SECONDS)
    with _lock:
        all_data = _load_all()
        sprint_map = all_data.get(key) if isinstance(all_data.get(key), dict) else {}
        sprint_map = _purge_expired(sprint_map, now)
        current = sprint_map.get(section)
        if not isinstance(current, dict) or current.get("token") != token:
            return {
                "ok": False,
                "section": section,
                "message": "锁已失效或不属于当前编辑者",
                "lock": _public_lock(current if isinstance(current, dict) else None),
                "locks": {s: _public_lock(sprint_map.get(s)) for s in SECTIONS},
            }
        current = {
            **current,
            "updatedAt": now_beijing_iso(),
            "expiresAt": expires.isoformat().replace("+00:00", "Z"),
        }
        sprint_map[section] = current
        all_data[key] = sprint_map
        _save_all(all_data)
        return {
            "ok": True,
            "section": section,
            "lock": _public_lock(current),
            "locks": {s: _public_lock(sprint_map.get(s)) for s in SECTIONS},
        }


def release_lock(
    sprint: str,
    *,
    section: str,
    token: str,
) -> dict[str, Any]:
    if section not in SECTIONS:
        raise ValueError(f"未知分区: {section}")
    token = (token or "").strip()
    key = _safe_name(sprint)
    now = _utc_now()
    with _lock:
        all_data = _load_all()
        sprint_map = all_data.get(key) if isinstance(all_data.get(key), dict) else {}
        sprint_map = _purge_expired(sprint_map, now)
        current = sprint_map.get(section)
        released = False
        if isinstance(current, dict) and current.get("token") == token:
            sprint_map.pop(section, None)
            released = True
        if sprint_map:
            all_data[key] = sprint_map
        else:
            all_data.pop(key, None)
        _save_all(all_data)
        return {
            "released": released,
            "section": section,
            "locks": {s: _public_lock(sprint_map.get(s)) for s in SECTIONS},
        }

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import settings
from .timeutil import now_beijing_iso

OVERRIDE_DIR = settings.db_path.parent / "overrides"


def _safe_name(sprint: str) -> str:
    name = (sprint or "").strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or "_unknown"


def override_path(sprint: str) -> Path:
    return OVERRIDE_DIR / f"{_safe_name(sprint)}.json"


def empty_override(sprint: str = "") -> dict[str, Any]:
    return {
        "sprint": sprint or "",
        "updatedAt": None,
        "testEnv": "",
        "riskBlock": "",
        "stories": {},
        "reopenRows": None,
    }


def load_override(sprint: str) -> dict[str, Any]:
    path = override_path(sprint)
    base = empty_override(sprint)
    if not path.exists():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base
    if not isinstance(data, dict):
        return base
    base["sprint"] = data.get("sprint") or sprint
    base["updatedAt"] = data.get("updatedAt")
    base["testEnv"] = data.get("testEnv") or ""
    base["riskBlock"] = data.get("riskBlock") or ""
    stories = data.get("stories") or {}
    base["stories"] = stories if isinstance(stories, dict) else {}
    # None = use Feishu snapshot reopen; list = manual replace (may be empty)
    if "reopenRows" in data:
        rows = data.get("reopenRows")
        if rows is None:
            base["reopenRows"] = None
        elif isinstance(rows, list):
            base["reopenRows"] = rows
        else:
            base["reopenRows"] = []
    else:
        base["reopenRows"] = None
    return base


def save_override(sprint: str, payload: dict[str, Any]) -> dict[str, Any]:
    current = load_override(sprint)
    now = now_beijing_iso()

    if "testEnv" in payload:
        current["testEnv"] = str(payload.get("testEnv") or "")
    if "riskBlock" in payload:
        current["riskBlock"] = str(payload.get("riskBlock") or "")

    if "stories" in payload and isinstance(payload["stories"], dict):
        # Full replace of stories map when provided
        cleaned: dict[str, dict[str, str]] = {}
        for name, fields in payload["stories"].items():
            key = str(name or "").strip()
            if not key or not isinstance(fields, dict):
                continue
            entry: dict[str, str] = {}
            for f in ("ready", "readyDate", "comment"):
                if f in fields:
                    entry[f] = str(fields.get(f) or "")
            if entry:
                cleaned[key] = entry
        current["stories"] = cleaned

    if "reopenRows" in payload:
        rows = payload.get("reopenRows")
        if rows is None:
            current["reopenRows"] = None
        elif isinstance(rows, list):
            cleaned_rows = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                summary = str(r.get("summary") or r.get("name") or "").strip()
                if not summary:
                    continue
                try:
                    times = int(r.get("reopenTimes") or 0)
                except (TypeError, ValueError):
                    times = 0
                cleaned_rows.append(
                    {
                        "priority": str(r.get("priority") or ""),
                        "status": str(r.get("status") or ""),
                        "summary": summary,
                        "name": summary,
                        "url": str(r.get("url") or ""),
                        "reopenTimes": times,
                    }
                )
            current["reopenRows"] = cleaned_rows

    current["sprint"] = sprint
    current["updatedAt"] = now
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    path = override_path(sprint)
    path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return current

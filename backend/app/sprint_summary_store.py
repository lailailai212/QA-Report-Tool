# -*- coding: utf-8 -*-
"""Persist parsed Sprint summary xlsx bundles per sprint."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import settings
from .timeutil import now_beijing_iso

SUMMARY_DIR = settings.db_path.parent / "sprint_summary"


def _safe(sprint: str) -> str:
    name = (sprint or "").strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or "_unknown"


def sprint_dir(sprint: str) -> Path:
    return SUMMARY_DIR / _safe(sprint)


def parsed_path(sprint: str) -> Path:
    return sprint_dir(sprint) / "parsed.json"


def meta_path(sprint: str) -> Path:
    return sprint_dir(sprint) / "meta.json"


def save_parsed(sprint: str, data: dict[str, Any], *, source_files: dict[str, str] | None = None) -> dict[str, Any]:
    d = sprint_dir(sprint)
    d.mkdir(parents=True, exist_ok=True)
    parsed_path(sprint).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    meta = {
        "sprint": sprint,
        "updatedAt": now_beijing_iso(),
        "sourceFiles": source_files or {},
    }
    meta_path(sprint).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def load_parsed(sprint: str) -> dict[str, Any] | None:
    p = parsed_path(sprint)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("incompleteItems")
    if isinstance(items, list):
        data["incompleteItems"] = [
            r for r in items if isinstance(r, dict) and r.get("type") == "User Story"
        ]
    return data


def load_meta(sprint: str) -> dict[str, Any]:
    p = meta_path(sprint)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def list_cached_sprints() -> list[str]:
    if not SUMMARY_DIR.exists():
        return []
    out: list[str] = []
    for p in SUMMARY_DIR.iterdir():
        if p.is_dir() and (p / "parsed.json").exists():
            out.append(p.name)
    return sorted(out)


def store_upload(sprint: str, field: str, filename: str, content: bytes) -> Path:
    d = sprint_dir(sprint) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{field}_{filename}"
    dest.write_bytes(content)
    return dest

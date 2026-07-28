from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .config import settings
from .timeutil import now_beijing_iso

OVERRIDE_DIR = settings.db_path.parent / "overrides"

# Canonical Test ENV options (order preserved when saving / displaying)
TEST_ENV_OPTIONS = ("SIT", "UAT1", "UAT2", "PRE1", "PRE2", "PROD")

SECTIONS = ("meta", "stories", "reopen")

_sprint_locks: dict[str, threading.Lock] = {}
_sprint_locks_guard = threading.Lock()


class OverrideConflictError(Exception):
    """Optimistic concurrency conflict on one override section."""

    def __init__(
        self,
        *,
        section: str,
        expected: int,
        actual: int,
        current: dict[str, Any],
    ) -> None:
        self.section = section
        self.expected = expected
        self.actual = actual
        self.current = current
        super().__init__(
            f"override section '{section}' conflict: expected rev {expected}, actual {actual}"
        )


def parse_test_env_tags(text: str | None) -> list[str]:
    """Parse free-text / comma-separated ENV into ordered known tags (+ unknowns)."""
    raw = str(text or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[,，、;/|\s]+", raw) if p.strip()]
    known_map = {o.upper(): o for o in TEST_ENV_OPTIONS}
    found: set[str] = set()
    unknowns: list[str] = []
    seen_unknown: set[str] = set()
    for p in parts:
        key = p.upper()
        if key in known_map:
            found.add(known_map[key])
        elif p not in seen_unknown:
            seen_unknown.add(p)
            unknowns.append(p)
    return [o for o in TEST_ENV_OPTIONS if o in found] + unknowns


def format_test_env(tags: list[str] | None) -> str:
    """Serialize tags to stable comma-separated storage string."""
    if not tags:
        return ""
    return ", ".join(parse_test_env_tags(", ".join(str(t) for t in tags)))


def _safe_name(sprint: str) -> str:
    name = (sprint or "").strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or "_unknown"


def override_path(sprint: str) -> Path:
    return OVERRIDE_DIR / f"{_safe_name(sprint)}.json"


def empty_rev() -> dict[str, int]:
    return {"meta": 0, "stories": 0, "reopen": 0}


def normalize_rev(raw: Any) -> dict[str, int]:
    base = empty_rev()
    if not isinstance(raw, dict):
        return base
    for key in SECTIONS:
        try:
            base[key] = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            base[key] = 0
    return base


def empty_override(sprint: str = "") -> dict[str, Any]:
    return {
        "sprint": sprint or "",
        "updatedAt": None,
        "rev": empty_rev(),
        "testEnv": "",
        "riskBlock": "",
        "stories": {},
        "reopenRows": None,
    }


def _lock_for(sprint: str) -> threading.Lock:
    key = _safe_name(sprint)
    with _sprint_locks_guard:
        lock = _sprint_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _sprint_locks[key] = lock
        return lock


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


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
    base["rev"] = normalize_rev(data.get("rev"))
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


def _touched_sections(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if "testEnv" in payload or "riskBlock" in payload:
        out.append("meta")
    if "stories" in payload:
        out.append("stories")
    if "reopenRows" in payload:
        out.append("reopen")
    return out


def save_override(
    sprint: str,
    payload: dict[str, Any],
    *,
    expected_rev: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Merge payload into sprint override with section-level optimistic concurrency.

    If expected_rev contains a section key that is being written, its value must
    match the current on-disk rev for that section; otherwise OverrideConflictError.
    """
    with _lock_for(sprint):
        current = load_override(sprint)
        rev = normalize_rev(current.get("rev"))
        touching = _touched_sections(payload)
        if not touching:
            return current

        expected = expected_rev if isinstance(expected_rev, dict) else {}
        for section in touching:
            if section not in expected or expected[section] is None:
                continue
            try:
                want = int(expected[section])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"expectedRev.{section} 无效") from exc
            actual = rev[section]
            if want != actual:
                raise OverrideConflictError(
                    section=section,
                    expected=want,
                    actual=actual,
                    current=current,
                )

        now = now_beijing_iso()

        if "testEnv" in payload:
            current["testEnv"] = format_test_env(
                parse_test_env_tags(payload.get("testEnv"))
            )
        if "riskBlock" in payload:
            current["riskBlock"] = str(payload.get("riskBlock") or "")

        if "stories" in payload and isinstance(payload["stories"], dict):
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
                try:
                    from .feishu_snapshot import (
                        enrich_reopen_rows_with_urls,
                        load_all_snapshot_bugs,
                    )

                    cleaned_rows = enrich_reopen_rows_with_urls(
                        cleaned_rows,
                        load_all_snapshot_bugs(prefer_sprint=sprint),
                    )
                except Exception:  # noqa: BLE001
                    pass
                current["reopenRows"] = cleaned_rows

        for section in touching:
            rev[section] = int(rev[section]) + 1
        current["rev"] = rev
        current["sprint"] = sprint
        current["updatedAt"] = now
        _atomic_write_json(override_path(sprint), current)
        return current

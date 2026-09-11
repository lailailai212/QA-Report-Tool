"""QC Story Point board: roster + Feishu QC Owner / Task / Tech Improvement."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import settings
from .override_store import load_override, normalize_points, save_override
from .retro_refresh import PERSON_ROLE_OVERRIDE, load_retro_snapshot
from .timeutil import now_beijing_iso

ROSTER_PATH = settings.db_path.parent / "qc_points_roster.json"

# Default QC roster (screenshot 0824迭代). Initial Points are hand-maintained.
DEFAULT_ROSTER: list[dict[str, Any]] = [
    {"name": "谢茜", "initialPoints": 5.0},
    {"name": "陈彦任", "initialPoints": 7.0},
    {"name": "张峰", "initialPoints": 7.0},
    {"name": "张蒙", "initialPoints": 7.0},
    {"name": "张瑞瑞", "initialPoints": 7.0},
    {"name": "李向丹", "initialPoints": 7.0},
    {"name": "梁贤丹", "initialPoints": 7.0},
    {"name": "魏来", "initialPoints": 5.0},
    {"name": "Yena Wang（王杰）", "initialPoints": 7.0},
]

DEFAULT_INITIAL = 7.0

# 飞书 QC Owner 英文名 ↔ 中文名，归并到飞书展示名「英文（中文）」
NAME_CANONICAL = {
    "Yena Wang": "Yena Wang（王杰）",
    "王杰": "Yena Wang（王杰）",
    "Yena Wang（王杰）": "Yena Wang（王杰）",
}


def _canon_name(name: str) -> str:
    n = (name or "").strip()
    return NAME_CANONICAL.get(n, n) if n else ""


def _round(n: float) -> float:
    return round(float(n) + 1e-9, 2)


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sprint_short_title(sprint: str) -> str:
    text = (sprint or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d{8})", text)
    if match:
        day = match.group(1)
        return f"{day[4:8]}迭代"
    return text


def load_roster() -> list[dict[str, Any]]:
    if not ROSTER_PATH.exists():
        return [dict(row) for row in DEFAULT_ROSTER]
    try:
        data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [dict(row) for row in DEFAULT_ROSTER]
    people = data.get("people") if isinstance(data, dict) else None
    if not isinstance(people, list) or not people:
        return [dict(row) for row in DEFAULT_ROSTER]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in people:
        if not isinstance(row, dict):
            continue
        name = _canon_name(str(row.get("name") or "").strip())
        if not name or name in seen:
            continue
        seen.add(name)
        initial = _as_float(row.get("initialPoints"), DEFAULT_INITIAL)
        out.append({"name": name, "initialPoints": _round(initial)})
    return out or [dict(row) for row in DEFAULT_ROSTER]


def save_roster(people: list[dict[str, Any]]) -> None:
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in people:
        name = _canon_name(str(row.get("name") or "").strip())
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(
            {
                "name": name,
                "initialPoints": _round(_as_float(row.get("initialPoints"), DEFAULT_INITIAL)),
            }
        )
    ROSTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updatedAt": now_beijing_iso(), "people": cleaned}
    tmp = ROSTER_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(ROSTER_PATH)


def qc_roster_names() -> set[str]:
    """Display names + aliases for people on the maintained QC roster."""
    names: set[str] = set()
    for row in load_roster():
        raw = str(row.get("name") or "").strip()
        if not raw:
            continue
        names.add(raw)
        names.add(_canon_name(raw))
    for alias, canon in NAME_CANONICAL.items():
        if alias in names or canon in names:
            names.add(alias)
            names.add(canon)
    for name, role in PERSON_ROLE_OVERRIDE.items():
        if role != "QC":
            continue
        names.add(name)
        names.add(_canon_name(name))
    names.discard("")
    return names


def is_qc_roster_person(name: str) -> bool:
    """Task Owner in the QC roster (or forced QC) counts as QC extra work."""
    n = (name or "").strip()
    if not n:
        return False
    forced = PERSON_ROLE_OVERRIDE.get(n) or PERSON_ROLE_OVERRIDE.get(_canon_name(n))
    if forced in {"LEAD", "DEV"}:
        return False
    if forced == "QC":
        return True
    roster = qc_roster_names()
    return n in roster or _canon_name(n) in roster


def _owners(row: dict[str, Any], *keys: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for key in keys:
        raw = row.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for item in raw:
            name = _canon_name(str(item or "").strip())
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _share(owners: list[str], pts: float) -> list[tuple[str, float]]:
    names = [n for n in owners if n]
    if not names:
        return []
    if not pts:
        return [(n, 0.0) for n in names]
    share = pts / len(names)
    return [(n, share) for n in names]


def _item_brief(
    row: dict[str, Any],
    *,
    kind: str,
    points: float,
) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "summary": str(row.get("summary") or "").strip(),
        "status": str(row.get("status") or "").strip(),
        "type": kind,
        "points": _round(points),
        "url": str(row.get("url") or ""),
    }


def _join_notes(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in items:
        summary = str(item.get("summary") or "").strip()
        if not summary or summary in seen:
            continue
        seen.add(summary)
        parts.append(summary)
    return "；".join(parts)


def _qc_people_from_snapshot(
    stories: list[dict[str, Any]],
    tis: list[dict[str, Any]],
) -> set[str]:
    names: set[str] = set()
    for row in stories:
        names.update(_owners(row, "qcOwners"))
    for row in tis:
        names.update(_owners(row, "qcOwners"))
    return names


def build_points_board(sprint: str) -> dict[str, Any]:
    sprint = (sprint or "").strip()
    if not sprint:
        raise ValueError("sprint 不能为空")

    snap = load_retro_snapshot(sprint)
    override = load_override(sprint)
    roster = load_roster()
    saved = normalize_points(override.get("points"))
    saved_map: dict[str, dict[str, Any]] = {}
    for p in saved.get("people") or []:
        if not isinstance(p, dict):
            continue
        name = _canon_name(str(p.get("name") or "").strip())
        if not name:
            continue
        prev = saved_map.get(name)
        if prev is None or (prev.get("hidden") and not p.get("hidden")):
            saved_map[name] = {**p, "name": name}

    stories = [r for r in (snap or {}).get("stories") or [] if isinstance(r, dict)]
    tasks = [r for r in (snap or {}).get("tasks") or [] if isinstance(r, dict)]
    tis = [r for r in (snap or {}).get("techImprovements") or [] if isinstance(r, dict)]

    story_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    story_pts: dict[str, float] = defaultdict(float)
    other_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    other_pts: dict[str, float] = defaultdict(float)

    qc_set = {p["name"] for p in roster} | set(saved_map) | _qc_people_from_snapshot(stories, tis)

    for row in stories:
        pts = _as_float(row.get("pointQc"))
        owners = _owners(row, "qcOwners")
        for name, share in _share(owners, pts):
            if share <= 0:
                continue
            story_pts[name] += share
            story_items[name].append(_item_brief(row, kind="User Story", points=share))

    for row in tis:
        pts = _as_float(row.get("pointQc"))
        owners = _owners(row, "qcOwners")
        for name, share in _share(owners, pts):
            if name not in qc_set:
                qc_set.add(name)
            if share <= 0:
                continue
            other_pts[name] += share
            other_items[name].append(
                _item_brief(row, kind="Tech Improvement", points=share)
            )

    for row in tasks:
        pts = _as_float(row.get("pointDev"))
        owners = _owners(row, "taskOwners")
        for name, share in _share(owners, pts):
            if name not in qc_set:
                continue
            if share <= 0:
                continue
            other_pts[name] += share
            other_items[name].append(_item_brief(row, kind="Task", points=share))

    ordered: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        n = _canon_name(name)
        if not n or n in seen:
            return
        seen.add(n)
        ordered.append(n)

    for row in saved.get("people") or []:
        if row.get("hidden"):
            continue
        _add(str(row.get("name") or ""))
    hidden_canon = {
        _canon_name(str(row.get("name") or ""))
        for row in saved.get("people") or []
        if row.get("hidden")
    }
    for n in hidden_canon:
        if n and n not in seen:
            seen.add(n)
    for row in roster:
        _add(str(row.get("name") or ""))
    for name in sorted(story_pts) + sorted(other_pts):
        _add(name)

    roster_map = {str(r["name"]): r for r in roster}
    people: list[dict[str, Any]] = []
    totals = {
        "initial": 0.0,
        "sprintTasks": 0.0,
        "otherPoints": 0.0,
        "regression": 0.0,
        "rollback": 0.0,
        "remaining": 0.0,
    }

    for name in ordered:
        ov = saved_map.get(name) or {}
        auto_note = _join_notes(other_items.get(name) or [])
        auto_other = _round(other_pts.get(name) or 0.0)
        sprint_tasks = _round(story_pts.get(name) or 0.0)

        if ov.get("initialPoints") is not None:
            initial = _round(_as_float(ov.get("initialPoints"), DEFAULT_INITIAL))
        elif roster_map.get(name):
            initial = _round(_as_float(roster_map[name].get("initialPoints"), DEFAULT_INITIAL))
        else:
            initial = DEFAULT_INITIAL

        note_manual = bool(ov.get("otherNoteManual"))
        pts_manual = bool(ov.get("otherPointsManual"))
        other_note = str(ov.get("otherNote") or "") if note_manual else auto_note
        other_points = (
            _round(_as_float(ov.get("otherPoints"), 0.0)) if pts_manual else auto_other
        )
        regression = _round(_as_float(ov.get("regression"), 0.0))
        rollback = _round(_as_float(ov.get("rollback"), 0.0))
        remaining = _round(initial - sprint_tasks - other_points - regression - rollback)

        people.append(
            {
                "name": name,
                "initialPoints": initial,
                "sprintTasks": sprint_tasks,
                "otherNote": other_note,
                "otherNoteAuto": auto_note,
                "otherNoteManual": note_manual,
                "otherPoints": other_points,
                "otherPointsAuto": auto_other,
                "otherPointsManual": pts_manual,
                "regression": regression,
                "rollback": rollback,
                "remaining": remaining,
                "stories": story_items.get(name) or [],
                "otherItems": other_items.get(name) or [],
            }
        )
        totals["initial"] += initial
        totals["sprintTasks"] += sprint_tasks
        totals["otherPoints"] += other_points
        totals["regression"] += regression
        totals["rollback"] += rollback
        totals["remaining"] += remaining

    for key in list(totals):
        totals[key] = _round(totals[key])

    return {
        "sprint": sprint,
        "title": sprint_short_title(sprint),
        "feishuSprint": (snap or {}).get("feishuSprint") or sprint,
        "snapshotExists": bool(snap),
        "fetchedAt": (snap or {}).get("fetchedAt"),
        "stories": len(stories),
        "tasks": len(tasks),
        "techImprovements": len(tis),
        "rev": (override.get("rev") or {}).get("points", 0),
        "updatedAt": override.get("updatedAt"),
        "rules": {
            "sprintTasks": "User Story 的 Story Point (QC)，按 QC Owner 归属；多人均分",
            "otherTasks": "Task 的 Story Points（Task Owner 且在 QC 花名册）+ Tech Improvement 的 Story Points (QC)",
            "remaining": "剩余 = 初始 − 迭代任务 − 其他预计 − 回归 − 回滚演练",
        },
        "totals": totals,
        "people": people,
    }


def save_points_board(
    sprint: str,
    people: list[dict[str, Any]],
    *,
    expected_rev: int | None = None,
) -> dict[str, Any]:
    current = build_points_board(sprint)
    visible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in people:
        if not isinstance(row, dict):
            continue
        name = _canon_name(str(row.get("name") or "").strip())
        if not name or name in seen:
            continue
        seen.add(name)
        visible.append({**row, "name": name, "hidden": False})

    override = load_override(sprint)
    prev = normalize_points(override.get("points")).get("people") or []
    hidden_rows: list[dict[str, Any]] = []
    for row in prev:
        name = _canon_name(str(row.get("name") or "").strip())
        if not name or name in seen:
            continue
        if row.get("hidden"):
            seen.add(name)
            hidden_rows.append({**row, "name": name, "hidden": True})
    for row in current.get("people") or []:
        name = _canon_name(str(row.get("name") or "").strip())
        if not name or name in seen:
            continue
        seen.add(name)
        hidden_rows.append(
            {
                "name": name,
                "initialPoints": row.get("initialPoints"),
                "otherNote": row.get("otherNote") or "",
                "otherNoteManual": bool(row.get("otherNoteManual")),
                "otherPoints": row.get("otherPoints"),
                "otherPointsManual": bool(row.get("otherPointsManual")),
                "regression": row.get("regression") or 0,
                "rollback": row.get("rollback") or 0,
                "hidden": True,
            }
        )

    payload = {"people": visible + hidden_rows}
    expected = {"points": expected_rev} if expected_rev is not None else None
    saved = save_override(sprint, {"points": payload}, expected_rev=expected)
    roster_people = [
        {
            "name": str(row.get("name") or "").strip(),
            "initialPoints": _as_float(row.get("initialPoints"), DEFAULT_INITIAL),
        }
        for row in visible
    ]
    if roster_people:
        save_roster(roster_people)
    board = build_points_board(sprint)
    board["rev"] = (saved.get("rev") or {}).get("points", board.get("rev"))
    board["updatedAt"] = saved.get("updatedAt")
    return board

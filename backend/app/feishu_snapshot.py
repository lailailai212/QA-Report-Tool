from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .config import ROOT

SNAPSHOT_DIR = ROOT / "exports" / "feishu"

READY_YES_STATUSES = frozenset({"待测试", "测试中", "待验收"})
BUG_STATUS_COLS = (
    "To Do",
    "Fixing",
    "Confirming",
    "Clarifying",
    "Testing",
    "Done",
    "Closed",
)
PRIORITY_ROWS = ("P0", "P1", "P2", "P3")
CLOSED_LIKE = frozenset({"done", "closed"})

# MS 子计划 ↔ 飞书 Story 标题匹配：规范化后全等，或相似度 ≥ 阈值
STORY_MATCH_MIN_RATIO = 0.88
# 第一、第二名分数差小于此值视为歧义，放弃匹配以免错挂
STORY_MATCH_AMBIGUITY_DELTA = 0.03


def snapshot_path(sprint: str) -> Path:
    return SNAPSHOT_DIR / f"{sprint}_latest.json"


def load_feishu_snapshot(sprint: str) -> dict[str, Any] | None:
    path = snapshot_path(sprint)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def normalize_story_title(name: str) -> str:
    """Match-oriented title key: casefold, NFKC, collapse space, strip MS [id] prefix."""
    s = (name or "").strip()
    if not s:
        return ""
    # MeterSphere 偶发前缀：[100274] Story Name
    s = re.sub(r"^\[\d+\]\s*", "", s)
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def story_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Exact original-name index (legacy). Prefer match_feishu_story for merges."""
    out: dict[str, dict[str, Any]] = {}
    for s in snapshot.get("stories") or []:
        name = (s.get("name") or "").strip()
        if name:
            out[name] = s
    return out


def match_feishu_story(
    plan_name: str,
    stories: list[dict[str, Any]],
    *,
    min_ratio: float = STORY_MATCH_MIN_RATIO,
) -> dict[str, Any] | None:
    """
    Resolve Feishu story for an MS plan name.
    1) normalize + exact
    2) else best SequenceMatcher ratio ≥ min_ratio (default 0.88)
    Ambiguous top-2 → None.
    """
    needle = normalize_story_title(plan_name)
    if not needle:
        return None

    exact: list[dict[str, Any]] = []
    scored: list[tuple[float, dict[str, Any]]] = []
    for s in stories:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        hay = normalize_story_title(name)
        if not hay:
            continue
        if hay == needle:
            exact.append(s)
            continue
        ratio = SequenceMatcher(None, needle, hay).ratio()
        if ratio >= min_ratio:
            scored.append((ratio, s))

    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        # Identical normalized titles — pick first (rare duplicate summaries)
        return exact[0]

    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1].get("name") or ""))
    best_ratio, best = scored[0]
    if len(scored) > 1:
        second = scored[1][0]
        if best_ratio - second < STORY_MATCH_AMBIGUITY_DELTA:
            return None
    return best


def match_override_story(
    plan_name: str,
    stories_ov: dict[str, Any],
    *,
    min_ratio: float = STORY_MATCH_MIN_RATIO,
) -> dict[str, Any]:
    """Lookup Sprint Ready override by MS plan name (exact, then fuzzy on keys)."""
    if not stories_ov:
        return {}
    if plan_name in stories_ov:
        return stories_ov[plan_name] or {}
    needle = normalize_story_title(plan_name)
    if not needle:
        return {}
    exact_key = None
    scored: list[tuple[float, str]] = []
    for key in stories_ov:
        hay = normalize_story_title(key)
        if hay == needle:
            exact_key = key
            break
        ratio = SequenceMatcher(None, needle, hay).ratio()
        if ratio >= min_ratio:
            scored.append((ratio, key))
    if exact_key is not None:
        return stories_ov.get(exact_key) or {}
    if not scored:
        return {}
    scored.sort(key=lambda x: (-x[0], x[1]))
    best_ratio, best_key = scored[0]
    if len(scored) > 1 and best_ratio - scored[1][0] < STORY_MATCH_AMBIGUITY_DELTA:
        return {}
    return stories_ov.get(best_key) or {}


def _norm_status(status: str) -> str:
    s = (status or "").strip()
    mapping = {
        "to do": "To Do",
        "testing": "Testing",
        "测试中": "Testing",
        "fixing": "Fixing",
        "confirming": "Confirming",
        "clarifying": "Clarifying",
        "done": "Done",
        "closed": "Closed",
    }
    return mapping.get(s.lower(), s)


def _norm_priority(priority: str) -> str:
    p = (priority or "").strip().upper()
    if p in PRIORITY_ROWS:
        return p
    return "P3" if not p else p


def _bug_title(bug: dict[str, Any]) -> str:
    return (bug.get("summary") or bug.get("name") or "").strip()


def aggregate_bugs(bugs: list[dict[str, Any]]) -> dict[str, Any]:
    # normalize title field used by email template
    normalized: list[dict[str, Any]] = []
    for raw in bugs:
        b = dict(raw)
        title = _bug_title(b)
        if title:
            b["summary"] = title
            b.setdefault("name", title)
        normalized.append(b)
    bugs = normalized

    matrix: dict[str, dict[str, int]] = {
        p: {c: 0 for c in BUG_STATUS_COLS} for p in PRIORITY_ROWS
    }
    other_status_count = 0
    for b in bugs:
        pri = _norm_priority(str(b.get("priority") or ""))
        if pri not in matrix:
            matrix[pri] = {c: 0 for c in BUG_STATUS_COLS}
            if pri not in PRIORITY_ROWS:
                # keep unknown priorities in matrix but not in display rows
                pass
        st = _norm_status(str(b.get("status") or ""))
        if st in BUG_STATUS_COLS:
            if pri in matrix:
                matrix[pri][st] = matrix[pri].get(st, 0) + 1
        else:
            other_status_count += 1

    rows = []
    totals = {c: 0 for c in BUG_STATUS_COLS}
    grand = 0
    for p in PRIORITY_ROWS:
        counts = matrix.get(p, {c: 0 for c in BUG_STATUS_COLS})
        row_total = sum(counts.get(c, 0) for c in BUG_STATUS_COLS)
        fixed = counts.get("Done", 0) + counts.get("Closed", 0)
        fixed_rate = f"{round(fixed * 100.0 / row_total)}%" if row_total else ""
        rows.append(
            {
                "priority": p,
                "counts": {c: counts.get(c, 0) for c in BUG_STATUS_COLS},
                "total": row_total,
                "fixedRate": fixed_rate,
            }
        )
        for c in BUG_STATUS_COLS:
            totals[c] += counts.get(c, 0)
        grand += row_total

    fixed_all = totals["Done"] + totals["Closed"]
    reopen_rows = [
        b
        for b in bugs
        if int(b.get("reopenTimes") or 0) > 0
    ]
    reopen_rows.sort(
        key=lambda x: (-int(x.get("reopenTimes") or 0), str(x.get("priority") or ""))
    )

    p0p1_rows = []
    for b in bugs:
        pri = _norm_priority(str(b.get("priority") or ""))
        st = (b.get("status") or "").strip().lower()
        if pri in {"P0", "P1"} and st not in CLOSED_LIKE:
            p0p1_rows.append(b)
    _pri_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    p0p1_rows.sort(
        key=lambda b: (
            _pri_rank.get(_norm_priority(str(b.get("priority") or "")), 99),
            str(b.get("summary") or b.get("name") or ""),
        )
    )

    return {
        "total": len(bugs),
        "statusColumns": list(BUG_STATUS_COLS),
        "matrixRows": rows,
        "matrixTotals": {
            "counts": totals,
            "total": grand,
            "fixedRate": (
                f"{round(fixed_all * 100.0 / grand)}%" if grand else ""
            ),
        },
        "otherStatusCount": other_status_count,
        "reopenRows": reopen_rows,
        "p0p1Rows": p0p1_rows,
    }


def derive_ready(status: str) -> str:
    return "Yes" if (status or "").strip() in READY_YES_STATUSES else "No"


def derive_comment(ready_date: str, expected_ready_date: str) -> str:
    if not ready_date or not expected_ready_date:
        return ""
    try:
        rd = date.fromisoformat(ready_date[:10])
        ed = date.fromisoformat(expected_ready_date[:10])
    except ValueError:
        return ""
    return "提测Delay" if rd > ed else ""

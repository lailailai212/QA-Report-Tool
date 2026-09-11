# -*- coding: utf-8 -*-
"""Build Sprint 管理层总结报告：飞书当前快照 vs 计划会冻结 Plan + exec overrides."""
from __future__ import annotations

import base64
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ROOT, settings
from .override_store import load_override, save_override
from .points_service import is_qc_roster_person
from .retro_refresh import (
    PERSON_ROLE_OVERRIDE,
    RETRO_RULES,
    list_point_outliers,
    list_retro_latest_files,
    load_retro_plan,
    load_retro_snapshot,
)
from .sprint_summary_store import load_meta, save_parsed
from .sprint_xlsx_parser import default_xlsx_paths, parse_sprint_xlsx_bundle
from .timeutil import now_beijing_iso
from . import sprint_summary_charts as charts

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

DEFAULT_SECTION_LEADS: dict[str, str] = {}

DEFAULT_SUMMARY_KV: dict[str, str] = {}

DEFAULT_GOALS: list[dict[str, str]] = []
DEFAULT_RISKS: list[dict[str, str]] = []
DEFAULT_MGMT: list[dict[str, str]] = []
DEFAULT_NEXT: dict[str, str] = {}
DEFAULT_METRIC_INSIGHTS: list[dict[str, str]] = []
DEFAULT_CAPACITY_FACTORS: list[dict[str, str]] = []

STATUS_LABEL = {"ok": "正常", "low": "低风险", "warn": "中风险", "danger": "高风险"}
GOAL_STATUS_LABEL = {
    "ok": "已完成",
    "late": "已完成但晚于计划",
    "warn": "部分完成",
    "danger": "未完成",
}
RISK_STATUS_LABEL = {"ok": "正常", "low": "低风险", "warn": "中风险", "danger": "未达预期"}
STORY_GATE_DONE = {
    str(s).strip() for s in (RETRO_RULES.get("itemDoneStatuses") or ["已验收", "待验收"])
}
SCOPE_SEED_PATH = ROOT / "docs" / "_tmp_sprint_scope_changes.json"
PROD_ENVS = frozenset({"PRD", "PROD", "PRODUCTION", "生产", "生产环境"})


def _join_people(names: Any) -> str:
    if isinstance(names, str):
        seq = [names]
    elif isinstance(names, (list, tuple)):
        seq = list(names)
    else:
        seq = []
    out: list[str] = []
    seen: set[str] = set()
    for n in seq:
        s = str(n or "").strip()
        if not s or s in {"—", "-"}:
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return "、".join(out)


def _story_owner_display(row: dict[str, Any]) -> tuple[str, str, str]:
    product = _join_people(
        row.get("pmOwners") or row.get("productOwners") or row.get("productOwner")
    )
    dev = _join_people(
        row.get("devOwners")
        or row.get("devOwner")
        or ([row.get("owner")] if row.get("owner") else [])
    )
    qc = _join_people(row.get("qcOwners") or row.get("qcOwner"))
    return product, dev, qc


def _sprint_window(sprint: str) -> str:
    parts = re.findall(r"(\d{8})", sprint or "")
    if len(parts) < 2:
        return ""
    a, b = parts[0], parts[1]

    def fmt(d: str) -> str:
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"

    return f"{fmt(a)}—{fmt(b)}"


def _kpi_tone(rate: float | None, target: float) -> str:
    if rate is None:
        return ""
    if rate >= target:
        return "ok"
    if rate >= target * 0.85:
        return "warn"
    return "danger"


def _sprint_start_key(name: str) -> tuple[int, str]:
    m = re.search(r"(\d{8})", name or "")
    if m:
        return int(m.group(1)), name
    return 0, name


def _snapshot_sprint_names() -> list[str]:
    names = {
        path.name.replace("_latest.json", "")
        for path in list_retro_latest_files()
    }
    return sorted(names, key=_sprint_start_key)


def list_summary_sprints() -> list[str]:
    """Newest-first sprints that already have a 飞书复盘快照."""
    return list(reversed(_snapshot_sprint_names()))


def _resolve_previous_sprint(sprint: str) -> str | None:
    sprint = (sprint or "").strip()
    if not sprint:
        return None
    ordered = _snapshot_sprint_names()
    if sprint not in ordered:
        ordered = sorted(set(ordered) | {sprint}, key=_sprint_start_key)
    idx = ordered.index(sprint)
    if idx <= 0:
        return None
    return ordered[idx - 1]


def _export_dir_candidates() -> list[Path]:
    out: list[Path] = []
    raw = getattr(settings, "sprint_summary_xlsx_dir", "") or ""
    if raw:
        out.append(Path(raw))
    out.append(load_default_xlsx_dir())
    return out


def _try_import_sibling_dir_for_sprint(target: str) -> bool:
    target = (target or "").strip()
    if not target:
        return False
    seen_dirs: set[str] = set()
    for base in _export_dir_candidates():
        candidates: list[Path] = []
        if base.is_dir():
            candidates.append(base)
        parent = base.parent
        if parent.is_dir():
            candidates.extend([c for c in parent.iterdir() if c.is_dir()])
        for child in candidates:
            key = str(child.resolve())
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            try:
                paths = default_xlsx_paths(child)
                if any(not p.exists() for p in paths.values()):
                    continue
                data = parse_sprint_xlsx_bundle(
                    user_story=paths["user_story"],
                    tech_improvement=paths["tech_improvement"],
                    task=paths["task"],
                    bug=paths["bug"],
                    subtasks=paths["subtasks"],
                )
                sp = str(data.get("sprint") or "").strip()
                if sp != target:
                    continue
                save_parsed(
                    sp,
                    data,
                    source_files={k: str(v) for k, v in paths.items()},
                )
                return True
            except Exception:
                continue
    return False


def _ensure_parsed_cache(sprint_name: str) -> dict[str, Any] | None:
    """Sprint 总结主数据：飞书复盘快照（当前状态），范围对比走 Plan 冻结基线。"""
    sprint_name = (sprint_name or "").strip()
    if not sprint_name:
        return None
    return _parsed_from_retro_snapshot(sprint_name)


def summary_data_status(sprint: str = "") -> dict[str, Any]:
    """Toolbar status: current snapshot vs frozen Plan, no xlsx."""
    from .retro_refresh import plan_status as retro_plan_status

    cached = list_summary_sprints()
    sprint = (sprint or "").strip()
    if not sprint:
        return {"cached": cached}
    snap = load_retro_snapshot(sprint)
    parsed = _parsed_from_retro_snapshot(sprint) if snap else None
    plan = retro_plan_status(sprint)
    auto = _auto_scope_from_retro(sprint) if snap and plan.get("exists") else None
    return {
        "cached": cached,
        "sprint": sprint,
        "exists": bool(parsed),
        "source": "retro_snapshot" if parsed else None,
        "fetchedAt": (snap or {}).get("fetchedAt") if snap else None,
        "plan": plan,
        "scope": {
            "hasPlan": bool(plan.get("exists")),
            "frozenAt": plan.get("frozenAt"),
            "movedIn": len((auto or {}).get("movedIn") or []),
            "movedOut": len((auto or {}).get("movedOut") or []),
        },
        "metrics": (parsed or {}).get("metrics") if parsed else None,
    }


def _delivery_from_type_block(block: dict[str, Any]) -> dict[str, Any]:
    total = int(block.get("total") or 0)
    done = int(block.get("doneCount") or 0)
    unfinished = max(0, total - done)
    rate = round(done * 100.0 / total, 1) if total else None
    return {
        "total": total,
        "done": done,
        "unfinished": unfinished,
        "doneRate": rate,
        "doneRateLabel": f"{rate}%" if rate is not None else None,
    }


def _count_status(rows: list[dict[str, Any]], wanted: set[str]) -> int:
    return sum(1 for r in rows if (r.get("status") or "").strip() in wanted)


def _is_prod_env(raw: str) -> bool:
    return (raw or "").strip().upper() in PROD_ENVS


def _rate(n: float, d: float) -> float | None:
    if not d:
        return None
    return round(n * 100.0 / d, 1)


def _role_points_from_blocks(
    sb: dict[str, Any], tb: dict[str, Any], tib: dict[str, Any]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for typ, block in (
        ("User Story", sb),
        ("Task", tb),
        ("Tech Improvement", tib),
    ):
        pd = float(block.get("planDev") or 0)
        pq = float(block.get("planQc") or 0)
        dd = float(block.get("doneDev") or 0)
        dq = float(block.get("doneQc") or 0)
        out[typ] = {
            "planDev": round(pd, 1),
            "planQc": round(pq, 1),
            "doneDev": round(dd, 1),
            "doneQc": round(dq, 1),
            "devRate": _rate(dd, pd),
            "qcRate": _rate(dq, pq),
        }
    pd = sum(out[t]["planDev"] for t in out)
    pq = sum(out[t]["planQc"] for t in out)
    dd = sum(out[t]["doneDev"] for t in out)
    dq = sum(out[t]["doneQc"] for t in out)
    out["total"] = {
        "planDev": round(pd, 1),
        "planQc": round(pq, 1),
        "doneDev": round(dd, 1),
        "doneQc": round(dq, 1),
        "devRate": _rate(dd, pd),
        "qcRate": _rate(dq, pq),
        "planTotal": round(pd + pq, 1),
        "doneTotal": round(dd + dq, 1),
        "totalRate": _rate(dd + dq, pd + pq),
    }
    return out


def _people_from_retro(
    stories: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    tis: list[dict[str, Any]],
) -> tuple[dict[str, int | None], int]:
    dev: set[str] = set()
    qc: set[str] = set()
    for r in stories + tis:
        for n in r.get("devOwners") or []:
            if str(n).strip():
                dev.add(str(n).strip())
        for n in r.get("qcOwners") or []:
            if str(n).strip():
                qc.add(str(n).strip())
        owner = str(r.get("owner") or "").strip()
        if owner:
            dev.add(owner)
    for r in tasks:
        for n in r.get("devOwners") or []:
            if str(n).strip():
                if is_qc_roster_person(str(n)):
                    qc.add(str(n).strip())
                else:
                    dev.add(str(n).strip())
        owner = str(r.get("owner") or "").strip()
        if owner:
            if is_qc_roster_person(owner):
                qc.add(owner)
            else:
                dev.add(owner)
    _apply_person_role_override_sets(dev, qc)
    return {"DEV": len(dev), "QC": len(qc), "PM": None}, len(dev | qc)


def _apply_person_role_override_sets(dev: set[str], qc: set[str]) -> None:
    for name, role in PERSON_ROLE_OVERRIDE.items():
        appeared = name in dev or name in qc
        if not appeared:
            continue
        if role == "QC":
            dev.discard(name)
            qc.add(name)
        elif role == "DEV":
            qc.discard(name)
            dev.add(name)
        elif role == "LEAD":
            dev.discard(name)
            qc.discard(name)


def _qc_owner_names(*row_groups: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for rows in row_groups:
        for r in rows:
            for n in r.get("qcOwners") or []:
                if str(n).strip():
                    names.add(str(n).strip())
    return names


def _split_hr_by_role(
    hr: list[dict[str, Any]], qc_names: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    personal_dev: list[dict[str, Any]] = []
    personal_qc: list[dict[str, Any]] = []
    for row in hr:
        name = str(row.get("owner") or "").strip()
        if not name or name == "（空）":
            continue
        forced = PERSON_ROLE_OVERRIDE.get(name)
        if forced == "LEAD":
            continue
        if forced == "QC" or (forced is None and name in qc_names):
            personal_qc.append(row)
        else:
            personal_dev.append(row)
    return personal_dev, personal_qc


def _sprint_workdays(sprint: str) -> int | None:
    parts = re.findall(r"(\d{8})", sprint or "")
    if len(parts) < 2:
        return None
    try:
        a = date(int(parts[0][0:4]), int(parts[0][4:6]), int(parts[0][6:8]))
        b = date(int(parts[1][0:4]), int(parts[1][4:6]), int(parts[1][6:8]))
    except ValueError:
        return None
    n = 0
    cur = a
    while cur <= b:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def _pp_delta_text(cur: float | None, prev: float | None) -> dict[str, str]:
    if cur is None or prev is None:
        return {"text": "—", "cls": "flat"}
    diff = round(cur - prev, 1)
    if abs(diff) < 0.05:
        return {"text": "0 pp", "cls": "flat"}
    sign = "+" if diff > 0 else ""
    return {
        "text": f"{sign}{diff} pp",
        "cls": "trend-up" if diff > 0 else "trend-down",
    }


def _auto_scope_from_retro(sprint: str) -> dict[str, Any] | None:
    from .retro_service import _scope_diff

    snap = load_retro_snapshot(sprint)
    plan = load_retro_plan(sprint)
    if not snap or not plan:
        return None
    diff = _scope_diff(plan, snap)
    if not diff.get("hasPlan"):
        return None
    summary: dict[str, Any] = {}
    for row in diff.get("types") or []:
        churn = row.get("churnRate") or ""
        summary[str(row.get("type") or "")] = {
            "plan": row.get("planCount") or 0,
            "movedIn": row.get("movedIn") or 0,
            "movedOut": row.get("movedOut") or 0,
            "churnRate": churn if isinstance(churn, str) else f"{churn}%",
        }
    snap_at = str(snap.get("fetchedAt") or "")
    src_at = str(plan.get("sourceFetchedAt") or "")
    frozen = str(diff.get("frozenAt") or "")
    note = f"计划会冻结于 {frozen or '—'}"
    if src_at:
        note += f"（基线快照 {src_at}）"
    if snap_at:
        note += f"，对比当前快照 {snap_at}"
    return {
        "hasPlan": True,
        "frozenAt": frozen,
        "sourceFetchedAt": src_at,
        "snapshotFetchedAt": snap_at,
        "movedIn": list(diff.get("movedIn") or []),
        "movedOut": list(diff.get("movedOut") or []),
        "summary": summary,
        "planNote": note,
    }


def _compact_from_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
    m = parsed.get("metrics") or {}
    rp_all = parsed.get("rolePoints") or {}
    rp = rp_all.get("total") or {}
    deliv = parsed.get("delivery") or {}
    ppl = parsed.get("peopleByRole") or {}
    return {
        "sprint": parsed.get("sprint") or "",
        "source": parsed.get("source") or "",
        "acceptRate": m.get("acceptRate"),
        "acceptLabel": m.get("acceptLabel"),
        "acceptClosed": m.get("acceptClosed"),
        "acceptTotal": m.get("acceptTotal"),
        "pointRate": m.get("pointRate"),
        "pointDone": m.get("pointDone"),
        "pointPlan": m.get("pointPlan"),
        "bugFound": m.get("bugTotal"),
        "bugFixed": m.get("bugFixed"),
        "bugFixRate": m.get("bugFixRate"),
        "normDqs": m.get("normDqs"),
        "baseDqs": m.get("baseDqs"),
        "doneDev": rp.get("doneDev"),
        "doneQc": rp.get("doneQc"),
        "planDev": rp.get("planDev"),
        "planQc": rp.get("planQc"),
        "peopleDev": ppl.get("DEV"),
        "peopleQc": ppl.get("QC"),
        "us": deliv.get("User Story") or {},
        "task": deliv.get("Task") or {},
        "ti": deliv.get("Tech Improvement") or {},
        "rpUs": rp_all.get("User Story") or {},
        "rpTask": rp_all.get("Task") or {},
        "rpTi": rp_all.get("Tech Improvement") or {},
        "rpTotal": rp,
        "severity": parsed.get("severity") or {},
    }


def _history_series(
    sprint: str, current: dict[str, Any], *, limit: int = 4
) -> list[dict[str, Any]]:
    ordered = _snapshot_sprint_names()
    if sprint not in ordered:
        ordered = sorted(set(ordered) | {sprint}, key=_sprint_start_key)
    idx = ordered.index(sprint)
    names = ordered[max(0, idx + 1 - limit) : idx + 1]
    out: list[dict[str, Any]] = []
    for name in names:
        parsed = current if name == sprint else _ensure_parsed_cache(name)
        if not parsed:
            continue
        row = _compact_from_parsed(parsed)
        row["sprint"] = name
        out.append(row)
    return out


def _parsed_from_retro_snapshot(sprint_name: str) -> dict[str, Any] | None:
    """Build dashboard-compatible metrics from exports/retro/*_latest.json."""
    from .retro_service import _bug_analysis, _personal_bugs, _personal_points_by_role, _type_block

    snap = load_retro_snapshot(sprint_name)
    if not snap:
        return None
    stories = [r for r in (snap.get("stories") or []) if isinstance(r, dict)]
    tasks = [r for r in (snap.get("tasks") or []) if isinstance(r, dict)]
    tis = [r for r in (snap.get("techImprovements") or []) if isinstance(r, dict)]
    bugs = [r for r in (snap.get("bugs") or []) if isinstance(r, dict)]
    if not stories and not tasks and not tis:
        return None

    sb = _type_block("User Story", stories)
    tb = _type_block("Task", tasks)
    tib = _type_block("Tech Improvement", tis)
    bug_a = _bug_analysis(bugs)

    item_total = len(stories)
    item_accept = _count_status(stories, {"已验收"})
    accept_rate = _rate(item_accept, item_total)

    role_pts = _role_points_from_blocks(sb, tb, tib)
    rp_total = role_pts["total"]
    point_rate = rp_total.get("totalRate")
    done_dev = float(rp_total.get("doneDev") or 0)
    norm_dqs = round(float(bug_a["baseDqs"]) / done_dev, 2) if done_dev else None
    bug_fix_rate = _parse_metric_number(bug_a.get("fixedRate"))

    severity: dict[str, dict[str, int]] = {}
    for row in bug_a.get("severityRows") or []:
        if not isinstance(row, dict):
            continue
        sev = str(row.get("severity") or "").strip()
        if not sev or sev == "（空）":
            continue
        severity[sev] = {
            "found": int(row.get("count") or 0),
            "fixed": int(row.get("fixed") or 0),
        }

    root_counts: dict[str, int] = {}
    for row in bug_a.get("rootCauseRows") or []:
        if not isinstance(row, dict):
            continue
        root_counts[str(row.get("rootCause") or "TBD")] = int(row.get("count") or 0)

    personal_dev, personal_qc = _personal_points_by_role(stories, tasks, tis)
    people_by_role = {
        "DEV": len(personal_dev),
        "QC": len(personal_qc),
        "PM": None,
    }
    people_count = len(personal_dev) + len(personal_qc)
    incomplete = []
    for r in stories:
        if str(r.get("status") or "").strip() in STORY_GATE_DONE:
            continue
        product, dev, qc = _story_owner_display(r)
        incomplete.append(
            {
                "id": r.get("id") or "",
                "summary": r.get("summary") or "",
                "status": r.get("status") or "",
                "type": "User Story",
                "url": r.get("url") or "",
                "owner": dev,
                "productOwner": product,
                "devOwner": dev,
                "qcOwner": qc,
                "pointDev": r.get("pointDev") or 0,
                "pointQc": r.get("pointQc") or 0,
            }
        )
    incomplete.sort(
        key=lambda r: (
            str(r.get("status") or ""),
            str(r.get("productOwner") or ""),
            str(r.get("devOwner") or ""),
            str(r.get("id") or ""),
        )
    )

    return {
        "sprint": sprint_name,
        "source": "retro_snapshot",
        "fetchedAt": snap.get("fetchedAt") or "",
        "stories": stories,
        "tasks": tasks,
        "techImprovements": tis,
        "bugs": bugs,
        "metrics": {
            "acceptClosed": item_accept,
            "acceptTotal": item_total,
            "acceptRate": accept_rate,
            "acceptLabel": f"{item_accept}/{item_total}",
            "pointDone": rp_total.get("doneTotal"),
            "pointPlan": rp_total.get("planTotal"),
            "pointRate": point_rate,
            "bugFixed": int(bug_a.get("fixed") or 0),
            "bugTotal": int(bug_a.get("total") or 0),
            "bugFixRate": bug_fix_rate,
            "baseDqs": bug_a.get("baseDqs"),
            "normDqs": norm_dqs,
            "validBugs": bug_a.get("validBugs"),
        },
        "delivery": {
            "User Story": _delivery_from_type_block(sb),
            "Task": _delivery_from_type_block(tb),
            "Tech Improvement": _delivery_from_type_block(tib),
        },
        "rolePoints": role_pts,
        "severity": severity,
        "rootCause": root_counts,
        "incompleteItems": incomplete,
        "peopleCount": people_count,
        "peopleByRole": people_by_role,
        "prodBugCount": sum(
            1 for b in bugs if _is_prod_env(str(b.get("environment") or ""))
        ),
        "personalDev": personal_dev,
        "personalQc": personal_qc,
        "personalBugs": _personal_bugs(bugs),
        "pointOutliers": list_point_outliers(
            ("User Story", stories),
            ("Task", tasks),
            ("Tech Improvement", tis),
        ),
    }


def _parse_metric_number(raw: Any) -> float | None:
    s = str(raw or "").strip()
    if not s or s in ("待填", "无导出", "—", "-"):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def _scope_deviation_high(raw: Any, threshold: float = 20.0) -> bool:
    n = _parse_metric_number(raw)
    return n is not None and n > threshold


def _preserve_multiline(text: Any) -> str:
    """Keep typed newlines; if missing, split jammed numbered items onto their own lines."""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    s = s.strip()
    if not s:
        return ""
    if "\n" not in s:
        s = re.sub(r"([。．.!?！？])[ \t]+(?=\d+[\.．、])", r"\1\n", s)
    return s


def _fmt_pct_line(rate: Any, done: Any = None, total: Any = None, label: str | None = None) -> str:
    if rate is None:
        return "—"
    if label:
        return f"{rate}%（{label}）"
    if done is not None and total is not None:
        return f"{rate}%（{done}/{total}）"
    return f"{rate}%"


def _critical_fix_rate(sev: dict[str, Any]) -> tuple[float | None, int, int]:
    crit = sev.get("Critical") or {}
    found = int(crit.get("found") or 0)
    fixed = int(crit.get("fixed") or 0)
    rate = round(fixed * 100.0 / found, 2) if found else None
    return rate, found, fixed


def _pp_delta(
    cur: float | None,
    prev: float | None,
    *,
    lower_is_better: bool = False,
) -> dict[str, str]:
    if cur is None or prev is None:
        return {"delta": "—", "deltaTone": "", "deltaDir": ""}
    diff = round(cur - prev, 2)
    if abs(diff) < 0.01:
        return {"delta": "0 pp", "deltaTone": "flat", "deltaDir": "flat"}
    improved = (diff < 0) if lower_is_better else (diff > 0)
    tone = "good" if improved else "bad"
    ddir = "up" if diff > 0 else "down"
    sign = "+" if diff > 0 else ""
    text = f"{sign}{diff} pp"
    if lower_is_better and improved:
        text += "（改善）"
    return {"delta": text, "deltaTone": tone, "deltaDir": ddir}


def _abs_delta(
    cur: float | None,
    prev: float | None,
    *,
    lower_is_better: bool = False,
    suffix: str = "",
) -> dict[str, str]:
    if cur is None or prev is None:
        return {"delta": "—", "deltaTone": "", "deltaDir": ""}
    diff = round(cur - prev, 2)
    if abs(diff) < 0.01:
        return {"delta": "0" + suffix, "deltaTone": "flat", "deltaDir": "flat"}
    improved = (diff < 0) if lower_is_better else (diff > 0)
    tone = "good" if improved else "bad"
    ddir = "up" if diff > 0 else "down"
    sign = "+" if diff > 0 else ""
    text = f"{sign}{diff}{suffix}"
    if lower_is_better and improved:
        text += "（改善）"
    return {"delta": text, "deltaTone": tone, "deltaDir": ddir}


def _merge_exec(
    parsed: dict[str, Any],
    ov: dict[str, Any],
    *,
    current_sprint: str = "",
    prev_sprint: str | None = None,
    prev_parsed: dict[str, Any] | None = None,
    auto_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ex = ov.get("exec") or {}
    leads = dict(DEFAULT_SECTION_LEADS)
    if isinstance(ex.get("sectionLeads"), dict):
        leads.update({str(k): str(v or "") for k, v in ex["sectionLeads"].items()})

    summary_kv = dict(DEFAULT_SUMMARY_KV)
    if isinstance(ex.get("summaryKv"), dict):
        summary_kv.update({str(k): str(v or "") for k, v in ex["summaryKv"].items()})

    goals = ex.get("goals") if isinstance(ex.get("goals"), list) else []
    risks = ex.get("risks") if isinstance(ex.get("risks"), list) else []
    mgmt = ex.get("mgmtRequests") if isinstance(ex.get("mgmtRequests"), list) else []
    metric_insights = (
        ex.get("metricInsights") if isinstance(ex.get("metricInsights"), list) else []
    )
    capacity_factors = (
        ex.get("capacityFactors") if isinstance(ex.get("capacityFactors"), list) else []
    )
    next_sp = dict(DEFAULT_NEXT)
    if isinstance(ex.get("nextSprint"), dict):
        next_sp.update({k: str(v or "") for k, v in ex["nextSprint"].items()})
    scope_in = ex.get("scopeMovedIn") if isinstance(ex.get("scopeMovedIn"), list) else []
    scope_out = ex.get("scopeMovedOut") if isinstance(ex.get("scopeMovedOut"), list) else []
    scope_sum = ex.get("scopeSummary") if isinstance(ex.get("scopeSummary"), dict) else {}
    if auto_scope and auto_scope.get("hasPlan"):
        # 有冻结 Plan 时，移入/移出始终按「Plan vs 当前快照」计算，不被上次保存的 0 覆盖。
        scope_in = list(auto_scope.get("movedIn") or [])
        scope_out = list(auto_scope.get("movedOut") or [])
        auto_sum = auto_scope.get("summary") or {}
        if isinstance(auto_sum, dict) and auto_sum:
            scope_sum = {
                str(typ): dict(row)
                for typ, row in auto_sum.items()
                if isinstance(row, dict)
            }
    prod = ex.get("productionMetrics") if isinstance(ex.get("productionMetrics"), dict) else {}
    delivery_scope_notes: dict[str, str] = {}
    if isinstance(ex.get("deliveryScopeNotes"), dict):
        delivery_scope_notes = {str(k): str(v or "") for k, v in ex["deliveryScopeNotes"].items()}

    incomplete = []
    for row in parsed.get("incompleteItems") or []:
        typ = str(row.get("type") or "").strip()
        if typ != "User Story":
            continue
        product, dev, qc = _story_owner_display(row)
        incomplete.append(
            {
                **row,
                "productOwner": product,
                "devOwner": dev,
                "qcOwner": qc,
                "owner": dev or str(row.get("owner") or "").strip(),
            }
        )

    m = parsed.get("metrics") or {}
    rp_total = (parsed.get("rolePoints") or {}).get("total") or {}
    ti = (parsed.get("delivery") or {}).get("Tech Improvement") or {}
    if not summary_kv.get("delivery"):
        summary_kv["delivery"] = (
            f"开发完成 {rp_total.get('doneDev', 0)}/{rp_total.get('planDev', 0)} 点"
            f"（{rp_total.get('devRate', '—')}%），测试完成 {rp_total.get('doneQc', 0)}/"
            f"{rp_total.get('planQc', 0)} 点（{rp_total.get('qcRate', '—')}%）。"
            f"合计 {m.get('pointDone')}/{m.get('pointPlan')}（{m.get('pointRate')}%）"
        )
    if not summary_kv.get("quality"):
        sev = parsed.get("severity") or {}
        crit = sev.get("Critical") or {}
        cf, cx = crit.get("found", 0), crit.get("fixed", 0)
        crit_rate = round(cx * 100.0 / cf, 1) if cf else None
        rc = parsed.get("rootCause") or {}
        rc_total = sum(rc.values()) or 1
        tbd = rc.get("TBD", 0) + rc.get("TBD（未填）", 0)
        tbd_pct = round(tbd * 100.0 / rc_total, 1)
        summary_kv["quality"] = (
            f"缺陷 {m.get('bugTotal')}、修复 {m.get('bugFixed')}（{m.get('bugFixRate')}%）；"
            f"Critical {cx}/{cf}（{crit_rate}%）；根因 TBD {tbd}/{rc_total}（{tbd_pct}%）；"
            f"基础 DQS {m.get('baseDqs')}，归一化 {m.get('normDqs')}"
        )

    one = str(ex.get("oneLiner") or "").strip()
    if not one:
        one = (
            f"验收闭环 {m.get('acceptLabel')}（{m.get('acceptRate')}%），"
            f"故事点 {m.get('pointDone')}/{m.get('pointPlan')}（{m.get('pointRate')}%）；"
            f"缺陷修复 {m.get('bugFixed')}/{m.get('bugTotal')}（{m.get('bugFixRate')}%），"
            f"归一化 DQS {m.get('normDqs')}。"
        )

    quality_score_detail = str(ex.get("qualityScoreDetail") or "").strip()
    if not quality_score_detail:
        prev_norm = None
        if prev_parsed:
            prev_norm = (prev_parsed.get("metrics") or {}).get("normDqs")
        prev_dqs_label = str(prev_norm) if prev_norm is not None else "无导出"
        quality_score_detail = (
            f"基础 DQS {m.get('baseDqs')} → 归一化 {m.get('normDqs')}（上期 {prev_dqs_label}）"
        )

    manual_compare = str(ex.get("compareSprint") or "").strip()
    sprint_label = str(parsed.get("sprint") or current_sprint or "").strip()
    current_label = (current_sprint or "").strip()
    if (
        manual_compare
        and manual_compare not in ("无导出", "—")
        and manual_compare != current_label
        and manual_compare != sprint_label
    ):
        compare_sprint = manual_compare
    elif prev_sprint:
        compare_sprint = prev_sprint
    else:
        compare_sprint = manual_compare or "无导出"

    team_composition = str(ex.get("teamComposition") or "").strip()
    ppl = parsed.get("peopleByRole") or {}
    if not team_composition:
        dev_n = ppl.get("DEV")
        qc_n = ppl.get("QC")
        pm_n = ppl.get("PM")
        team_composition = (
            f"DEV：{dev_n if dev_n is not None else '—'}人 | "
            f"QC：{qc_n if qc_n is not None else '—'}人 | "
            f"PM：{pm_n if pm_n is not None else '—'}人"
        )

    if auto_scope and auto_scope.get("hasPlan") and auto_scope.get("planNote"):
        plan_note = str(auto_scope.get("planNote") or "")
    else:
        plan_note = str(ex.get("scopePlanNote") or "").strip()

    workdays = ex.get("workdays")
    if workdays in (None, ""):
        workdays = _sprint_workdays(current_sprint or str(parsed.get("sprint") or ""))

    overall = str(ex.get("overallStatus") or "").strip()
    if overall not in {"ok", "low", "warn", "danger"}:
        overall = ""

    return {
        "overallStatus": overall,
        "overallStatusLabel": STATUS_LABEL.get(overall, ""),
        "oneLiner": one,
        "highlight": _preserve_multiline(ex.get("highlight")),
        "concern": _preserve_multiline(ex.get("concern")),
        "sectionLeads": leads,
        "summaryKv": summary_kv,
        "goals": goals,
        "risks": risks,
        "mgmtRequests": mgmt,
        "metricInsights": metric_insights,
        "capacityFactors": capacity_factors,
        "nextSprint": next_sp,
        "scopeMovedIn": scope_in,
        "scopeMovedOut": scope_out,
        "scopeSummary": scope_sum,
        "scopePlanNote": plan_note,
        "incompleteItems": incomplete,
        "compareSprint": compare_sprint,
        "reportOwner": str(ex.get("reportOwner") or "").strip(),
        "productionMetrics": prod,
        "workdays": workdays,
        "membersNote": str(ex.get("membersNote") or "").strip(),
        "teamComposition": team_composition,
        "qualityScoreDetail": quality_score_detail,
        "deliveryScopeNotes": delivery_scope_notes,
        "scopeAuto": bool(auto_scope and auto_scope.get("hasPlan")),
    }


def _build_dashboard_rows(
    parsed: dict[str, Any],
    prod: dict[str, str],
    *,
    prev_parsed: dict[str, Any] | None = None,
    prev_prod: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    m = parsed.get("metrics") or {}
    deliv = parsed.get("delivery") or {}
    sev = parsed.get("severity") or {}
    crit_rate, cf, cx = _critical_fix_rate(sev)
    us = deliv.get("User Story") or {}
    ti = deliv.get("Tech Improvement") or {}

    pm = (prev_parsed or {}).get("metrics") or {}
    pdeliv = (prev_parsed or {}).get("delivery") or {}
    psev = (prev_parsed or {}).get("severity") or {}
    prev_crit_rate, pcf, pcx = _critical_fix_rate(psev)
    pus = pdeliv.get("User Story") or {}
    pti = pdeliv.get("Tech Improvement") or {}
    prev_prod = prev_prod or {}

    no_export = "无导出"

    def row(
        name: str,
        current: str,
        target: str,
        status: str,
        *,
        prev: str = no_export,
        delta: str = "—",
        delta_tone: str = "",
        delta_dir: str = "",
        tone: str | None = None,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "current": current,
            "prev": prev,
            "delta": delta,
            "deltaTone": delta_tone,
            "deltaDir": delta_dir,
            "target": target,
            "status": status,
            "tone": tone or status,
        }

    def rate_row(
        name: str,
        cur_rate: float | None,
        cur_done: Any,
        cur_total: Any,
        cur_label: str | None,
        prev_rate: float | None,
        prev_done: Any,
        prev_total: Any,
        prev_label: str | None,
        target: str,
        status: str,
        tone: str | None = None,
        *,
        lower_is_better: bool = False,
    ) -> dict[str, Any]:
        cur_s = _fmt_pct_line(cur_rate, cur_done, cur_total, cur_label)
        if prev_parsed and prev_rate is not None:
            prev_s = _fmt_pct_line(prev_rate, prev_done, prev_total, prev_label)
            d = _pp_delta(cur_rate, prev_rate, lower_is_better=lower_is_better)
        else:
            prev_s = no_export
            d = {"delta": "—", "deltaTone": "", "deltaDir": ""}
        return row(
            name,
            cur_s,
            target,
            status,
            prev=prev_s,
            delta=d["delta"],
            delta_tone=d["deltaTone"],
            delta_dir=d["deltaDir"],
            tone=tone,
        )

    accept_cur = m.get("acceptRate")
    point_cur = m.get("pointRate")
    bug_cur = m.get("bugFixRate")
    dqs_cur = m.get("normDqs")
    try:
        dqs_cur_f = float(dqs_cur) if dqs_cur is not None else None
    except (TypeError, ValueError):
        dqs_cur_f = None
    prev_dqs = pm.get("normDqs")
    try:
        prev_dqs_f = float(prev_dqs) if prev_dqs is not None else None
    except (TypeError, ValueError):
        prev_dqs_f = None

    prod_cur = _parse_metric_number(prod.get("prodBugs"))
    prod_prev = _parse_metric_number(prev_prod.get("prodBugs"))
    auto_cur = _parse_metric_number(prod.get("automationCoverage"))
    auto_prev = _parse_metric_number(prev_prod.get("automationCoverage"))

    rows = [
        rate_row(
            "验收闭环率（Story）",
            accept_cur,
            m.get("acceptClosed"),
            m.get("acceptTotal"),
            m.get("acceptLabel"),
            pm.get("acceptRate"),
            pm.get("acceptClosed"),
            pm.get("acceptTotal"),
            pm.get("acceptLabel"),
            "80%",
            "warn" if (accept_cur or 0) < 80 else "ok",
            tone=_kpi_tone(accept_cur, 80),
        ),
        rate_row(
            "故事点完成率",
            point_cur,
            m.get("pointDone"),
            m.get("pointPlan"),
            None,
            pm.get("pointRate"),
            pm.get("pointDone"),
            pm.get("pointPlan"),
            None,
            "95%",
            "danger" if (point_cur or 0) < 95 else "ok",
            tone=_kpi_tone(point_cur, 95),
        ),
        rate_row(
            "User Story 完成率",
            us.get("doneRate"),
            us.get("done"),
            us.get("total"),
            None,
            pus.get("doneRate"),
            pus.get("done"),
            pus.get("total"),
            None,
            "85%",
            "warn" if (us.get("doneRate") or 0) < 85 else "ok",
            tone=_kpi_tone(us.get("doneRate"), 85),
        ),
        rate_row(
            "技改完成率",
            ti.get("doneRate"),
            ti.get("done"),
            ti.get("total"),
            None,
            pti.get("doneRate"),
            pti.get("done"),
            pti.get("total"),
            None,
            "60%",
            "danger" if (ti.get("doneRate") or 0) < 60 else "ok",
            tone=_kpi_tone(ti.get("doneRate"), 60),
        ),
    ]

    dqs_prev_s = (
        str(prev_dqs)
        if prev_parsed and prev_dqs is not None
        else no_export
    )
    dqs_d = (
        _abs_delta(dqs_cur_f, prev_dqs_f, lower_is_better=True)
        if prev_parsed and dqs_cur_f is not None and prev_dqs_f is not None
        else {"delta": "—", "deltaTone": "", "deltaDir": ""}
    )
    rows.append(
        row(
            "归一化 DQS",
            str(m.get("normDqs") or "—"),
            "< 5.0",
            "danger" if (m.get("normDqs") or 99) >= 5 else "ok",
            prev=dqs_prev_s,
            delta=dqs_d["delta"],
            delta_tone=dqs_d["deltaTone"],
            delta_dir=dqs_d["deltaDir"],
        )
    )

    rows.extend(
        [
            rate_row(
                "缺陷修复率",
                bug_cur,
                m.get("bugFixed"),
                m.get("bugTotal"),
                None,
                pm.get("bugFixRate"),
                pm.get("bugFixed"),
                pm.get("bugTotal"),
                None,
                "85%",
                "danger" if (bug_cur or 0) < 85 else "ok",
                tone=_kpi_tone(bug_cur, 85),
            ),
            rate_row(
                "Critical 修复率",
                crit_rate,
                cx,
                cf,
                None,
                prev_crit_rate,
                pcx,
                pcf,
                None,
                "100%",
                "danger" if crit_rate is not None and crit_rate < 100 else "ok",
            ),
        ]
    )

    reopen_cur_s = str(prod.get("reopenRate") or "").strip() or "待填"
    reopen_prev_s = str(prod.get("reopenRatePrev") or "").strip() or "待填"
    reopen_cur_n = _parse_metric_number(reopen_cur_s if reopen_cur_s != "待填" else "")
    reopen_prev_n = _parse_metric_number(reopen_prev_s if reopen_prev_s != "待填" else "")
    reopen_d = (
        _pp_delta(reopen_cur_n, reopen_prev_n, lower_is_better=True)
        if reopen_cur_n is not None and reopen_prev_n is not None
        else {"delta": "—", "deltaTone": "", "deltaDir": ""}
    )
    if reopen_cur_n is None:
        reopen_status = "待填"
        reopen_tone = "待填"
    elif reopen_cur_n < 5:
        reopen_status = "ok"
        reopen_tone = "ok"
    else:
        reopen_status = "danger"
        reopen_tone = "danger"
    rows.append(
        row(
            "Reopen 率",
            reopen_cur_s,
            "< 5%",
            reopen_status,
            prev=reopen_prev_s,
            delta=reopen_d["delta"],
            delta_tone=reopen_d["deltaTone"],
            delta_dir=reopen_d["deltaDir"],
            tone=reopen_tone,
        )
    )

    prod_d = (
        _abs_delta(prod_cur, prod_prev, lower_is_better=True)
        if prod_cur is not None and prod_prev is not None
        else {"delta": "—", "deltaTone": "", "deltaDir": ""}
    )
    rows.append(
        row(
            "生产缺陷数",
            prod.get("prodBugs") or "待填",
            "环比下降",
            prod.get("prodBugsStatus") or "待填",
            prev=str(prev_prod.get("prodBugs") or no_export) if prev_parsed else no_export,
            delta=prod_d["delta"],
            delta_tone=prod_d["deltaTone"],
            delta_dir=prod_d["deltaDir"],
        )
    )

    auto_d = (
        _pp_delta(auto_cur, auto_prev)
        if auto_cur is not None and auto_prev is not None
        else {"delta": "—", "deltaTone": "", "deltaDir": ""}
    )
    rows.append(
        row(
            "自动化测试覆盖率",
            prod.get("automationCoverage") or "待填",
            "待冻结",
            prod.get("automationCoverageStatus") or "待填",
            prev=str(prev_prod.get("automationCoverage") or no_export) if prev_parsed else no_export,
            delta=auto_d["delta"],
            delta_tone=auto_d["deltaTone"],
            delta_dir=auto_d["deltaDir"],
        )
    )
    return rows


def _status_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    c: Counter[str] = Counter()
    for r in rows:
        if not isinstance(r, dict):
            continue
        c[(r.get("status") or "").strip() or "（空）"] += 1
    return [{"status": k, "count": v} for k, v in sorted(c.items(), key=lambda x: (-x[1], x[0]))]


def _items_for_type(parsed: dict[str, Any], typ: str) -> list[dict[str, Any]]:
    key = {
        "User Story": "stories",
        "Task": "tasks",
        "Tech Improvement": "techImprovements",
    }.get(typ)
    if not key:
        return []
    return [r for r in (parsed.get(key) or []) if isinstance(r, dict)]


def _fmt_join_rates(values: list[Any], *, suffix: str = "%") -> str:
    parts: list[str] = []
    for v in values:
        if v is None or v == "":
            parts.append("—")
        else:
            parts.append(f"{v}{suffix}")
    return " → ".join(parts)


def _chart_captions(history: list[dict[str, Any]]) -> dict[str, str]:
    if not history:
        return {}
    last = history[-1]
    acc = _fmt_join_rates([h.get("acceptRate") for h in history])
    pts = _fmt_join_rates([h.get("pointRate") for h in history])
    bug_r = _fmt_join_rates([h.get("bugFixRate") for h in history])
    dqs = " → ".join(
        str(h.get("normDqs") if h.get("normDqs") is not None else "—") for h in history
    )
    return {
        "delivery": f"验收闭环 {acc}；故事点 {pts}。",
        "bugs": (
            f"修复率 {bug_r}。本期 Bug {last.get('bugFound') or 0}、"
            f"已修复 {last.get('bugFixed') or 0}。"
        ),
        "dqs": f"近四期归一化 DQS：{dqs}。",
        "points": (
            f"本期 DEV {last.get('doneDev') if last.get('doneDev') is not None else '—'}、"
            f"QC {last.get('doneQc') if last.get('doneQc') is not None else '—'}。"
        ),
        "capita": "刻度为人均完成点，不等于人天。",
    }


def _count_delta_line(cur: Any, prev: Any, *, label: str) -> str:
    try:
        c = float(cur)
        p = float(prev)
    except (TypeError, ValueError):
        return ""
    diff = round(c - p, 1)
    if abs(diff) < 0.05:
        return f"{label} 持平"
    sign = "+" if diff > 0 else ""
    if p:
        pct = round(diff * 100.0 / p, 1)
        return f"{label} {sign}{diff:g} 个（{sign}{pct}%）"
    return f"{label} {sign}{diff:g} 个（上期 0）"


def _pp_mom_line(cur: Any, prev: Any, *, label: str, lower_is_better: bool = False) -> dict[str, str]:
    d = _pp_delta_text(
        _parse_metric_number(cur) if not isinstance(cur, (int, float)) else float(cur),
        _parse_metric_number(prev) if not isinstance(prev, (int, float)) else float(prev),
    )
    arrow = "↑" if "pp" in d["text"] and d["text"].startswith("+") else (
        "↓" if d["text"].startswith("-") else ""
    )
    cls = d["cls"]
    if lower_is_better and cls == "trend-up":
        cls = "trend-down"
    elif lower_is_better and cls == "trend-down":
        cls = "trend-up"
    if arrow:
        text = f"{label} {arrow} {d['text'].replace(' pp', 'pp')}"
    else:
        text = f"{label} {d['text']}"
    return {"text": text, "cls": cls}


def _personal_tables(parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    from .retro_service import _personal_bugs, _personal_points_by_role

    stories = [r for r in (parsed.get("stories") or []) if isinstance(r, dict)]
    tasks = [r for r in (parsed.get("tasks") or []) if isinstance(r, dict)]
    tis = [r for r in (parsed.get("techImprovements") or []) if isinstance(r, dict)]
    bugs = [r for r in (parsed.get("bugs") or []) if isinstance(r, dict)]

    personal_dev, personal_qc = _personal_points_by_role(stories, tasks, tis)

    personal_bugs = [r for r in (parsed.get("personalBugs") or []) if isinstance(r, dict)]
    if not personal_bugs and any(str(b.get("owner") or "").strip() for b in bugs):
        personal_bugs = _personal_bugs(bugs)

    pts_map: dict[str, Any] = {}
    for r in personal_qc:
        pts_map[str(r.get("owner") or "")] = r.get("donePoint")
    for r in personal_dev:
        pts_map[str(r.get("owner") or "")] = r.get("donePoint")
    out_bugs: list[dict[str, Any]] = []
    for row in personal_bugs:
        owner = str(row.get("owner") or "")
        pts = pts_map.get(owner)
        try:
            pts_f = float(pts) if pts is not None else 0.0
        except (TypeError, ValueError):
            pts_f = 0.0
        try:
            base = float(row.get("baseDqs") or 0)
        except (TypeError, ValueError):
            base = 0.0
        if pts_f:
            norm: Any = round(base / pts_f, 2)
        elif base:
            norm = "#DIV/0!"
        else:
            norm = 0
        out_bugs.append(
            {
                **row,
                "donePoint": pts if pts is not None else 0,
                "normDqs": norm,
            }
        )
    return personal_dev, personal_qc, out_bugs


def _build_details(
    parsed: dict[str, Any],
    exec_block: dict[str, Any],
    *,
    prev_parsed: dict[str, Any] | None,
    prev_sprint: str | None,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    types = ("User Story", "Task", "Tech Improvement")
    scope_sum = exec_block.get("scopeSummary") or {}
    prev_scope = {}
    if prev_sprint:
        auto_prev = _auto_scope_from_retro(prev_sprint)
        if auto_prev and auto_prev.get("hasPlan"):
            prev_scope = auto_prev.get("summary") or {}
        else:
            prev_ov = load_override(prev_sprint)
            prev_scope = (prev_ov.get("exec") or {}).get("scopeSummary") or {}

    plan_blocks: list[dict[str, Any]] = []
    delivery_detail: list[dict[str, Any]] = []
    scope_mom: list[dict[str, Any]] = []
    pdeliv = (prev_parsed or {}).get("delivery") or {}
    for typ in types:
        rows = _items_for_type(parsed, typ)
        d = (parsed.get("delivery") or {}).get(typ) or {}
        pd = pdeliv.get(typ) or {}
        ss = scope_sum.get(typ) if isinstance(scope_sum.get(typ), dict) else {}
        pss = prev_scope.get(typ) if isinstance(prev_scope.get(typ), dict) else {}
        dist = _status_counts(rows)
        accepted = sum(1 for r in rows if (r.get("status") or "").strip() == "已验收")
        total = int(d.get("total") or len(rows) or 0)
        accept_gap = _rate(total - accepted, total) if total else None
        prev_rows = _items_for_type(prev_parsed or {}, typ) if prev_parsed else []
        prev_dist = _status_counts(prev_rows) if prev_parsed else []
        prev_accepted = sum(
            1 for r in prev_rows if (r.get("status") or "").strip() == "已验收"
        )
        prev_total = int(pd.get("total") or len(prev_rows) or 0)
        prev_gap = _rate(prev_total - prev_accepted, prev_total) if prev_total else None
        plan_blocks.append(
            {
                "type": typ,
                "plan": d.get("total"),
                "actual": d.get("done"),
                "doneRate": d.get("doneRate"),
                "statusDist": dist,
                "accepted": accepted,
                "acceptGap": accept_gap if typ == "User Story" else None,
                "acceptFocus": typ == "User Story",
                "scopePlan": ss.get("plan"),
                "movedIn": ss.get("movedIn"),
                "movedOut": ss.get("movedOut"),
                "churnRate": ss.get("churnRate"),
                "churnHigh": _scope_deviation_high(ss.get("churnRate")),
            }
        )
        rate_mom = _pp_delta_text(
            d.get("doneRate"), pd.get("doneRate") if prev_parsed else None
        )
        gap_mom = (
            _pp_mom_line(accept_gap, prev_gap, label="偏差率", lower_is_better=True)
            if typ == "User Story"
            else None
        )
        delivery_detail.append(
            {
                "type": typ,
                "curPlan": d.get("total"),
                "curActual": d.get("done"),
                "curRate": d.get("doneRate"),
                "prevPlan": pd.get("total") if prev_parsed else None,
                "prevActual": pd.get("done") if prev_parsed else None,
                "prevRate": pd.get("doneRate") if prev_parsed else None,
                "curDist": dist,
                "prevDist": prev_dist,
                "curGap": accept_gap if typ == "User Story" else None,
                "prevGap": prev_gap if typ == "User Story" else None,
                "acceptFocus": typ == "User Story",
                "rateMom": rate_mom,
                "gapMom": gap_mom,
                "actualDelta": _count_delta_line(d.get("done"), pd.get("done"), label="实际")
                if prev_parsed
                else "",
                "planDelta": _count_delta_line(d.get("total"), pd.get("total"), label="计划")
                if prev_parsed
                else "",
                "acceptedDelta": (
                    f"已验收 {accepted - prev_accepted:+d} 个" if prev_parsed else ""
                ),
            }
        )
        cur_in = _parse_metric_number(ss.get("movedIn")) or 0
        cur_out = _parse_metric_number(ss.get("movedOut")) or 0
        prev_in = _parse_metric_number(pss.get("movedIn")) or 0
        prev_out = _parse_metric_number(pss.get("movedOut")) or 0
        churn_mom = _pp_mom_line(
            ss.get("churnRate"), pss.get("churnRate") if pss else None, label="偏差率", lower_is_better=True
        )
        scope_mom.append(
            {
                "type": typ,
                "plan": ss.get("plan"),
                "movedIn": ss.get("movedIn"),
                "movedOut": ss.get("movedOut"),
                "churnRate": ss.get("churnRate"),
                "churnHigh": _scope_deviation_high(ss.get("churnRate")),
                "prevPlan": pss.get("plan"),
                "prevIn": pss.get("movedIn"),
                "prevOut": pss.get("movedOut"),
                "prevChurn": pss.get("churnRate"),
                "churnMom": churn_mom,
                "changeDelta": _count_delta_line(
                    cur_in + cur_out, prev_in + prev_out, label="变更"
                )
                if pss
                else "",
                "hasCur": _scope_summary_meaningful({typ: ss}) if ss else False,
                "hasPrev": _scope_summary_meaningful({typ: pss}) if pss else False,
            }
        )

    rp_map = {
        "User Story": "rpUs",
        "Task": "rpTask",
        "Tech Improvement": "rpTi",
    }
    hist_rev = list(reversed(history))
    points_split: list[dict[str, Any]] = []
    for typ, key in rp_map.items():
        plan_cells = []
        done_cells = []
        for h in hist_rev:
            block = h.get(key) or {}
            pdv = block.get("planDev")
            pq = block.get("planQc")
            dd = block.get("doneDev")
            dq = block.get("doneQc")
            try:
                plan_tot = round(float(pdv or 0) + float(pq or 0), 1)
                done_tot = round(float(dd or 0) + float(dq or 0), 1)
            except (TypeError, ValueError):
                plan_tot, done_tot = None, None
            rate = _rate(done_tot or 0, plan_tot or 0) if plan_tot else None
            plan_cells.append(
                {"dev": pdv, "qc": pq, "total": plan_tot, "rate": rate}
            )
            done_cells.append({"dev": dd, "qc": dq, "total": done_tot, "rate": None})
        points_split.append({"type": typ, "dim": "计划 Story Point", "cells": plan_cells})
        points_split.append({"type": "", "dim": "完成 Story Point", "cells": done_cells})
    rate_row = []
    for h in hist_rev:
        rate_row.append(
            {
                "dev": None,
                "qc": None,
                "total": f"{h.get('pointDone')}/{h.get('pointPlan')}",
                "rate": h.get("pointRate"),
            }
        )
    points_split.append({"type": "完成率", "dim": "", "cells": rate_row, "bold": True})

    personal_dev, personal_qc, personal_bugs = _personal_tables(parsed)
    return {
        "planBlocks": plan_blocks,
        "deliveryDetail": delivery_detail,
        "scopeMom": scope_mom,
        "pointsSplit": points_split,
        "histLabels": [
            "本期" if i == 0 else f"−{i}" for i in range(len(hist_rev))
        ],
        "personalDev": personal_dev,
        "personalQc": personal_qc,
        "personalBugs": personal_bugs,
        "typeTrendSvg": charts.type_trend_svg(history),
    }


def build_sprint_summary_report(sprint: str) -> dict[str, Any]:
    maybe_seed_exec_scope(sprint)
    parsed = _ensure_parsed_cache(sprint)
    if not parsed:
        raise FileNotFoundError(
            f"未找到 Sprint {sprint} 的飞书快照。请先点「刷新飞书」拉取当前状态。"
        )
    ov = load_override(sprint)
    meta = load_meta(sprint)
    prev_sprint = _resolve_previous_sprint(sprint)
    prev_parsed = _ensure_parsed_cache(prev_sprint) if prev_sprint else None
    prev_prod: dict[str, str] = {}
    if prev_sprint and prev_parsed:
        prev_ov = load_override(prev_sprint)
        prev_prod = (prev_ov.get("exec") or {}).get("productionMetrics") or {}
        if not isinstance(prev_prod, dict):
            prev_prod = {}
    auto_scope = _auto_scope_from_retro(sprint)
    exec_block = _merge_exec(
        parsed,
        ov,
        current_sprint=sprint,
        prev_sprint=prev_sprint,
        prev_parsed=prev_parsed,
        auto_scope=auto_scope,
    )

    order = ["Critical", "Major", "Medium", "Minor", "Trivial"]
    prev_sev = (prev_parsed or {}).get("severity") or {}
    sev_rows = []
    for sev in order:
        c = (parsed.get("severity") or {}).get(sev) or {}
        pc = prev_sev.get(sev) or {}
        found = int(c.get("found") or 0)
        fixed = int(c.get("fixed") or 0)
        pfound = int(pc.get("found") or 0)
        pfixed = int(pc.get("fixed") or 0)
        rate = round(fixed * 100.0 / found, 2) if found else None
        prate = round(pfixed * 100.0 / pfound, 2) if pfound else None
        row_cls = ""
        if sev == "Critical":
            row_cls = "row-danger"
        elif sev == "Major":
            row_cls = "row-warn"
        sev_rows.append(
            {
                "severity": sev,
                "found": found,
                "fixed": fixed,
                "fixRate": rate,
                "prevFound": pfound if prev_parsed else None,
                "prevFixed": pfixed if prev_parsed else None,
                "prevFixRate": prate if prev_parsed else None,
                "alert": bool(rate is not None and rate < 90 and sev in {"Critical", "Major"}),
                "rowClass": row_cls,
            }
        )
    tot_found = sum(r["found"] for r in sev_rows)
    tot_fixed = sum(r["fixed"] for r in sev_rows)
    tot_pf = sum(int(r.get("prevFound") or 0) for r in sev_rows)
    tot_px = sum(int(r.get("prevFixed") or 0) for r in sev_rows)
    sev_totals = {
        "found": tot_found,
        "fixed": tot_fixed,
        "fixRate": round(tot_fixed * 100.0 / tot_found, 2) if tot_found else None,
        "prevFound": tot_pf if prev_parsed else None,
        "prevFixed": tot_px if prev_parsed else None,
        "prevFixRate": round(tot_px * 100.0 / tot_pf, 2) if prev_parsed and tot_pf else None,
    }

    rc = parsed.get("rootCause") or {}
    rc_total = sum(rc.values()) or 1
    rc_rows = []
    for k, v in sorted(rc.items(), key=lambda x: -x[1]):
        pct = round(v * 100.0 / rc_total, 2)
        cause = str(k)
        rc_rows.append(
            {
                "cause": cause,
                "count": v,
                "pct": pct,
                "alert": "TBD" in cause.upper() and pct > 30,
            }
        )

    deliv = parsed.get("delivery") or {}
    rp = parsed.get("rolePoints") or {}
    rp_total = rp.get("total") or {}
    m = parsed.get("metrics") or {}
    pdeliv = (prev_parsed or {}).get("delivery") or {}
    pm = (prev_parsed or {}).get("metrics") or {}
    scope_sum = exec_block.get("scopeSummary") or {}

    delivery_rows = []
    for typ in ("User Story", "Task", "Tech Improvement"):
        d = deliv.get(typ) or {}
        pd = pdeliv.get(typ) or {}
        delta = _pp_delta_text(d.get("doneRate"), pd.get("doneRate") if prev_parsed else None)
        ss = scope_sum.get(typ) if isinstance(scope_sum.get(typ), dict) else {}
        churn_n = _parse_metric_number(ss.get("churnRate")) if ss else None
        delivery_rows.append(
            {
                "type": typ,
                "total": d.get("total", 0),
                "done": d.get("done", 0),
                "unfinished": d.get("unfinished", 0),
                "doneRate": d.get("doneRate"),
                "doneRateLabel": d.get("doneRateLabel"),
                "delta": delta["text"],
                "deltaCls": delta["cls"],
                "movedIn": ss.get("movedIn") if ss else None,
                "movedOut": ss.get("movedOut") if ss else None,
                "churnRate": ss.get("churnRate") if ss else None,
                "churnHigh": bool(churn_n is not None and churn_n > 20),
                "scopeReason": (exec_block.get("deliveryScopeNotes") or {}).get(typ) or "",
            }
        )
    acc_delta = _pp_delta_text(
        m.get("acceptRate"), pm.get("acceptRate") if prev_parsed else None
    )
    point_delta = _pp_delta_text(
        m.get("pointRate"), pm.get("pointRate") if prev_parsed else None
    )
    delivery_rows.append(
        {
            "type": "合计（Story 验收闭环）",
            "total": m.get("acceptTotal"),
            "done": f"{m.get('acceptClosed')} 已验收",
            "unfinished": (m.get("acceptTotal") or 0) - (m.get("acceptClosed") or 0),
            "doneRate": m.get("acceptRate"),
            "doneRateLabel": f"{m.get('acceptRate')}%",
            "delta": acc_delta["text"],
            "deltaCls": acc_delta["cls"],
            "bold": True,
        }
    )

    history = _history_series(sprint, parsed, limit=4)
    cur_people = parsed.get("peopleByRole") or {}
    for h in history:
        if not h.get("peopleDev") and cur_people.get("DEV"):
            h["peopleDev"] = cur_people.get("DEV")
        if not h.get("peopleQc") and cur_people.get("QC"):
            h["peopleQc"] = cur_people.get("QC")

    reopen_info = _reopen_stats(parsed, ov, prev_parsed, prev_sprint)
    prod_count = parsed.get("prodBugCount")
    if prod_count and not (exec_block.get("productionMetrics") or {}).get("prodBugs"):
        exec_block.setdefault("productionMetrics", {})
        exec_block["productionMetrics"]["prodBugs"] = str(prod_count)

    if not exec_block.get("overallStatus"):
        crit_rate, _cf, _cx = _critical_fix_rate(parsed.get("severity") or {})
        accept = m.get("acceptRate") or 0
        bug = m.get("bugFixRate") or 0
        if accept < 68 or bug < 72 or (crit_rate is not None and crit_rate < 85):
            overall = "danger"
        elif accept < 80 or (m.get("pointRate") or 0) < 95 or bug < 85:
            overall = "warn"
        else:
            overall = "ok"
        exec_block["overallStatus"] = overall
        exec_block["overallStatusLabel"] = STATUS_LABEL.get(overall, "")

    kpi_prev = _compact_from_parsed(prev_parsed) if prev_parsed else {}
    details_path = f"/sprint-summary/details?sprint={quote(sprint)}"
    details = _build_details(
        parsed,
        exec_block,
        prev_parsed=prev_parsed,
        prev_sprint=prev_sprint,
        history=history,
    )
    ppl = parsed.get("peopleByRole") or {}
    people_dev = len(details.get("personalDev") or [])
    people_qc = len(details.get("personalQc") or [])
    people_pm = ppl.get("PM")
    people_count = people_dev + people_qc
    done_total = float(rp_total.get("doneTotal") or 0)
    if people_count:
        raw_capita = done_total / people_count
        per_capita = round(raw_capita, 1 if raw_capita >= 1 else 2)
    else:
        per_capita = None

    has_plan = bool(auto_scope and auto_scope.get("hasPlan"))
    plan_frozen_at = str((auto_scope or {}).get("frozenAt") or "")
    snapshot_at = str(parsed.get("fetchedAt") or "")
    if has_plan:
        source_label = "飞书当前快照 vs 计划会冻结基线"
    else:
        source_label = "飞书当前快照（尚未冻结 Plan，范围偏差无法对比）"
    captions = _chart_captions(history)

    return {
        "title": "Sprint 总结报告",
        "sprint": sprint,
        "feishuSprint": parsed.get("sprint") or sprint,
        "sprintWindow": _sprint_window(parsed.get("sprint") or sprint),
        "generatedAt": now_beijing_iso(),
        "source": "retro_snapshot",
        "sourceLabel": source_label,
        "snapshotFetchedAt": snapshot_at,
        "planFrozenAt": plan_frozen_at,
        "hasPlan": has_plan,
        "meta": meta,
        "metrics": m,
        "delivery": deliv,
        "deliveryRows": delivery_rows,
        "pointDelta": point_delta,
        "rolePoints": rp,
        "severityRows": sev_rows,
        "severityTotals": sev_totals,
        "rootCauseRows": rc_rows,
        "prevSprint": prev_sprint,
        "prevSprintLoaded": bool(prev_parsed),
        "prevSprintSource": (prev_parsed or {}).get("source") if prev_parsed else "",
        "kpiPrev": kpi_prev,
        "exec": exec_block,
        "peopleCount": people_count,
        "peopleDev": people_dev,
        "peopleQc": people_qc,
        "peoplePm": people_pm,
        "perCapita": per_capita,
        "unfinishedPoints": round(
            (rp_total.get("planTotal") or 0) - (rp_total.get("doneTotal") or 0), 1
        ),
        "pointOutliers": parsed.get("pointOutliers") or [],
        "history": history,
        "charts": {
            "delivery": charts.delivery_trend_svg(history),
            "bugs": charts.bug_found_fixed_svg(history),
            "points": charts.role_points_svg(history),
            "capita": charts.per_capita_svg(history),
            "typeTrend": details.get("typeTrendSvg") or "",
        },
        "captions": captions,
        "reopen": reopen_info,
        "personalDev": details.get("personalDev") or [],
        "personalQc": details.get("personalQc") or [],
        "personalBugs": details.get("personalBugs") or [],
        "details": details,
        "detailsPath": details_path,
        "noExportLabel": "无导出",
        "validBugs": m.get("validBugs")
        if m.get("validBugs") is not None
        else sum(
            1
            for b in (parsed.get("bugs") or [])
            if isinstance(b, dict) and str(b.get("severity") or "").strip()
        ),
    }


def _reopen_stats(
    parsed: dict[str, Any],
    ov: dict[str, Any],
    prev_parsed: dict[str, Any] | None,
    prev_sprint: str | None,
) -> dict[str, Any]:
    bugs = [b for b in (parsed.get("bugs") or []) if isinstance(b, dict)]
    closed_n = sum(
        1 for b in bugs if str(b.get("status") or "").strip().lower() in {"closed", "done"}
    )
    rows = ov.get("reopenRows")
    cur_n: int | None = None
    cur_rate: float | None = None
    filled_rows: list[Any] = []
    if isinstance(rows, list):
        filled_rows = [
            r
            for r in rows
            if isinstance(r, dict) and str(r.get("summary") or "").strip()
        ]
        if filled_rows:
            cur_n = len(filled_rows)
            cur_rate = round(cur_n * 100.0 / closed_n, 1) if closed_n else 0.0
    prod = ((ov.get("exec") or {}).get("productionMetrics") or {}) if isinstance(ov.get("exec"), dict) else {}
    if cur_rate is None:
        cur_rate = _parse_metric_number(prod.get("reopenRate"))
    prev_rate = _parse_metric_number(prod.get("reopenRatePrev"))
    if prev_rate is None and prev_sprint:
        prev_ov = load_override(prev_sprint)
        prev_rows = prev_ov.get("reopenRows")
        prev_bugs = [b for b in ((prev_parsed or {}).get("bugs") or []) if isinstance(b, dict)]
        prev_closed = sum(
            1
            for b in prev_bugs
            if str(b.get("status") or "").strip().lower() in {"closed", "done"}
        )
        if prev_rows is not None and isinstance(prev_rows, list):
            pn = len(
                [
                    r
                    for r in prev_rows
                    if isinstance(r, dict) and str(r.get("summary") or "").strip()
                ]
            )
            if pn:
                prev_rate = round(pn * 100.0 / prev_closed, 1) if prev_closed else 0.0
    return {
        "count": cur_n,
        "closed": closed_n,
        "rate": cur_rate,
        "prevRate": prev_rate,
        "label": (
            f"{cur_rate}%" if cur_rate is not None else "待填"
        ),
        "detail": (
            f"{cur_n} / {closed_n}" if cur_n is not None else ""
        ),
        "rows": filled_rows,
    }

def render_sprint_summary_html(
    report: dict[str, Any],
    *,
    editable: bool = False,
    for_email: bool = False,
    chart_cids: dict[str, str] | None = None,
    chart_data_uris: dict[str, str] | None = None,
) -> str:
    tpl_name = (
        "sprint_summary_report_email.html" if for_email else "sprint_summary_report.html"
    )
    tpl = env.get_template(tpl_name)
    html = tpl.render(
        report=report,
        editable=editable,
        STATUS_LABEL=STATUS_LABEL,
        GOAL_STATUS_LABEL=GOAL_STATUS_LABEL,
        RISK_STATUS_LABEL=RISK_STATUS_LABEL,
        scope_deviation_high=_scope_deviation_high,
        chart_cids=chart_cids or {},
        chart_data_uris=chart_data_uris or {},
    )
    if not editable and not for_email:
        from .sprint_summary_publish_store import finalize_readonly_html

        html = finalize_readonly_html(html)
    return html


def render_sprint_summary_email(report: dict[str, Any]) -> tuple[str, dict[str, bytes]]:
    """HTML + PNG charts. SVG is stripped by Feishu; PNG uses CID and data-URI."""
    from .sprint_summary_chart_png import history_chart_pngs

    pngs = history_chart_pngs(report.get("history") or [])
    cids: dict[str, str] = {}
    data_uris: dict[str, str] = {}
    inline: dict[str, bytes] = {}
    for key, blob in pngs.items():
        cid = f"ss-chart-{key}"
        cids[key] = cid
        inline[cid] = blob
        data_uris[key] = base64.b64encode(blob).decode("ascii")
    html = render_sprint_summary_html(
        report, for_email=True, chart_cids=cids, chart_data_uris=data_uris
    )
    return html, inline


def render_sprint_summary_details_html(report: dict[str, Any]) -> str:
    tpl = env.get_template("sprint_summary_details.html")
    return tpl.render(report=report, scope_deviation_high=_scope_deviation_high)


def publish_sprint_summary_report(sprint: str) -> dict[str, Any]:
    """Build read-only HTML from current sprint data + overrides and store for sharing."""
    from .sprint_summary_publish_store import (
        new_publish_id,
        published_report_path,
        publish_html,
    )

    report = build_sprint_summary_report(sprint)
    html = render_sprint_summary_html(report, editable=False)
    title = str(report.get("title") or "Sprint 总结报告")
    feishu = report.get("feishuSprint") or sprint
    publish_id = new_publish_id()
    report["backPath"] = published_report_path(publish_id)
    details_html = render_sprint_summary_details_html(report)
    meta = publish_html(
        sprint,
        html,
        title=f"{title} · {feishu}",
        details_html=details_html,
        publish_id=publish_id,
    )
    return {
        **meta,
        "path": published_report_path(meta["publishId"]),
    }


def parse_and_cache_bundle(
    sprint: str,
    *,
    user_story: Path,
    tech_improvement: Path,
    task: Path,
    bug: Path,
    subtasks: Path,
    source_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    data = parse_sprint_xlsx_bundle(
        user_story=user_story,
        tech_improvement=tech_improvement,
        task=task,
        bug=bug,
        subtasks=subtasks,
    )
    sp = str(data.get("sprint") or sprint)
    save_parsed(sp, data, source_files=source_files)
    maybe_seed_exec_scope(sp)
    return data


def load_default_xlsx_dir() -> Path:
    raw = getattr(settings, "sprint_summary_xlsx_dir", "") or ""
    if raw:
        return Path(raw)
    return Path.home() / "Downloads" / "sprint数据导出-OBIS (31)"


def _scope_rows_meaningful(rows: Any) -> bool:
    if not isinstance(rows, list):
        return False
    for r in rows:
        if not isinstance(r, dict):
            continue
        iid = str(r.get("id") or "").strip()
        summary = str(r.get("summary") or "").strip()
        if iid and iid != "—":
            return True
        if summary:
            return True
    return False


def _scope_summary_meaningful(scope_sum: Any) -> bool:
    if not isinstance(scope_sum, dict) or not scope_sum:
        return False
    for ss in scope_sum.values():
        if not isinstance(ss, dict):
            continue
        for key in ("plan", "movedIn", "movedOut"):
            try:
                if int(ss.get(key) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        if _parse_metric_number(ss.get("churnRate")) is not None:
            return True
    return False


def maybe_seed_exec_scope(sprint: str) -> None:
    """Seed scope moved in/out from docs seed file when override has no real list data."""
    ov = load_override(sprint)
    ex = ov.get("exec") or {}
    if not SCOPE_SEED_PATH.exists():
        return
    try:
        seed = json.loads(SCOPE_SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    seed_sprint = str(seed.get("sprint") or "").strip()
    if seed_sprint and seed_sprint != sprint:
        return

    def _row(r: dict[str, Any]) -> dict[str, str]:
        iid = str(r.get("id") or "").strip()
        url = str(r.get("url") or "").strip()
        if not url and iid and iid != "—":
            url = f"https://project.feishu.cn/obis/workitem/{iid}"
        return {
            "type": str(r.get("type") or ""),
            "id": iid,
            "summary": str(r.get("summary") or ""),
            "url": url,
        }

    moved_in = [_row(r) for r in (seed.get("movedIn") or []) if isinstance(r, dict)]
    moved_out = [_row(r) for r in (seed.get("movedOut") or []) if isinstance(r, dict)]
    exec_patch: dict[str, Any] = {}
    if moved_in and not _scope_rows_meaningful(ex.get("scopeMovedIn")):
        exec_patch["scopeMovedIn"] = moved_in
    if moved_out and not _scope_rows_meaningful(ex.get("scopeMovedOut")):
        exec_patch["scopeMovedOut"] = moved_out
    seed_summary = seed.get("summary") or {}
    if seed_summary and not _scope_summary_meaningful(ex.get("scopeSummary")):
        exec_patch["scopeSummary"] = seed_summary
    plan_note = str(seed.get("planNote") or "").strip()
    if plan_note and not str(ex.get("scopePlanNote") or "").strip():
        exec_patch["scopePlanNote"] = plan_note
    if not exec_patch:
        return
    save_override(sprint, {"exec": exec_patch})


def import_default_xlsx_dir(base_dir: Path | None = None) -> dict[str, Any]:
    base = base_dir or load_default_xlsx_dir()
    paths = default_xlsx_paths(base)
    missing = [k for k, p in paths.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"目录 {base} 缺少文件: {', '.join(missing)}")
    data = parse_sprint_xlsx_bundle(
        user_story=paths["user_story"],
        tech_improvement=paths["tech_improvement"],
        task=paths["task"],
        bug=paths["bug"],
        subtasks=paths["subtasks"],
    )
    sp = str(data.get("sprint") or "")
    if not sp:
        raise ValueError("无法从 xlsx 识别 Sprint 名称")
    save_parsed(
        sp,
        data,
        source_files={k: str(v) for k, v in paths.items()},
    )
    maybe_seed_exec_scope(sp)
    return {"sprint": sp, "metrics": data.get("metrics"), "parsed": data}

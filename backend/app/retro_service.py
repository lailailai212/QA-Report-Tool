"""Sprint management retrospective report (Point / Severity / DQS)."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .override_store import load_override, normalize_retro
from .points_service import _canon_name, is_qc_roster_person
from .retro_refresh import (
    PERSON_ROLE_OVERRIDE,
    RETRO_RULES,
    is_lead_person,
    item_story_points,
    list_retro_latest_files,
    load_retro_plan,
    load_retro_snapshot,
)
from .timeutil import now_beijing_iso, now_beijing_mmdd

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

ITEM_DONE = set(RETRO_RULES["itemDoneStatuses"])
POINT_DONE = set(RETRO_RULES["pointDoneStatuses"])
BUG_FIXED = {s.lower() for s in RETRO_RULES["bugFixedStatuses"]}
SEVERITY_W = {k: float(v) for k, v in RETRO_RULES["severityWeights"].items()}
SEVERITY_ORDER = ["Critical", "Major", "Medium", "Minor", "Trivial"]


def _pct(n: float, d: float) -> str:
    if not d:
        return "N/A"
    return f"{round(n * 100.0 / d, 2)}%"


def _round(n: float) -> float:
    return round(float(n) + 1e-9, 2)


def _is_item_done(status: str) -> bool:
    return (status or "").strip() in ITEM_DONE


def _is_point_done(status: str) -> bool:
    return (status or "").strip() in POINT_DONE


def _is_bug_fixed(status: str) -> bool:
    return (status or "").strip().lower() in BUG_FIXED


def _status_dist(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    c = Counter((r.get("status") or "").strip() or "（空）" for r in rows)
    return [{"status": k, "count": v} for k, v in sorted(c.items(), key=lambda x: (-x[1], x[0]))]


def _type_block(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    done_n = sum(1 for r in rows if _is_item_done(str(r.get("status") or "")))
    plan_dev = 0.0
    plan_qc = 0.0
    done_dev = 0.0
    done_qc = 0.0
    for r in rows:
        pd, pq = item_story_points(r)
        done = _is_point_done(str(r.get("status") or ""))
        owner = str(r.get("owner") or "").strip()
        is_task = name == "Task"
        if is_task and pd and is_qc_roster_person(owner):
            pq += pd
            pd = 0.0
        if pd and not is_lead_person(owner):
            plan_dev += pd
            if done:
                done_dev += pd
        if pq:
            if is_task:
                qc_owner = owner
            else:
                qcs = r.get("qcOwners") or []
                qc_owner = str(qcs[0] if qcs else "").strip()
            if not is_lead_person(qc_owner):
                plan_qc += pq
                if done:
                    done_qc += pq
    unfinished = [
        {
            "id": r.get("id") or "",
            "summary": r.get("summary") or "",
            "status": r.get("status") or "",
            "type": name,
            "url": r.get("url") or "",
            "owner": r.get("owner") or "",
        }
        for r in rows
        if not _is_item_done(str(r.get("status") or ""))
    ]
    return {
        "type": name,
        "total": total,
        "doneCount": done_n,
        "doneRate": _pct(done_n, total),
        "statusDist": _status_dist(rows),
        "planDev": _round(plan_dev),
        "planQc": _round(plan_qc),
        "planTotal": _round(plan_dev + plan_qc),
        "doneDev": _round(done_dev),
        "doneQc": _round(done_qc),
        "doneTotal": _round(done_dev + done_qc),
        "pointDoneRate": _pct(done_dev + done_qc, plan_dev + plan_qc),
        "unfinished": unfinished,
    }


def _scope_diff(
    plan: dict[str, Any] | None, snap: dict[str, Any]
) -> dict[str, Any]:
    if not plan:
        return {
            "hasPlan": False,
            "types": [],
            "movedIn": [],
            "movedOut": [],
        }

    types_out: list[dict[str, Any]] = []
    moved_in: list[dict[str, str]] = []
    moved_out: list[dict[str, str]] = []

    for key, label in (
        ("stories", "User Story"),
        ("tasks", "Task"),
        ("techImprovements", "Tech Improvement"),
    ):
        plan_rows = [r for r in (plan.get(key) or []) if isinstance(r, dict)]
        cur_rows = [r for r in (snap.get(key) or []) if isinstance(r, dict)]
        plan_ids = {str(r.get("id") or "") for r in plan_rows if r.get("id")}
        cur_map = {
            str(r.get("id") or ""): r for r in cur_rows if r.get("id")
        }
        plan_map = {
            str(r.get("id") or ""): r for r in plan_rows if r.get("id")
        }
        cur_ids = set(cur_map)
        ins = sorted(cur_ids - plan_ids)
        outs = sorted(plan_ids - cur_ids)
        for iid in ins:
            r = cur_map[iid]
            moved_in.append(
                {
                    "id": iid,
                    "summary": str(r.get("summary") or ""),
                    "type": label,
                    "op": "移入",
                    "url": str(r.get("url") or ""),
                }
            )
        for iid in outs:
            r = plan_map[iid]
            moved_out.append(
                {
                    "id": iid,
                    "summary": str(r.get("summary") or ""),
                    "type": label,
                    "op": "移出",
                    "url": "",
                }
            )
        types_out.append(
            {
                "type": label,
                "planCount": len(plan_ids),
                "actualCount": len(cur_ids),
                "movedIn": len(ins),
                "movedOut": len(outs),
                "churnCount": len(ins) + len(outs),
                "churnRate": _pct(len(ins) + len(outs), len(plan_ids)),
            }
        )

    return {
        "hasPlan": True,
        "frozenAt": plan.get("frozenAt"),
        "types": types_out,
        "movedIn": moved_in,
        "movedOut": moved_out,
    }


def _bug_analysis(bugs: list[dict[str, Any]]) -> dict[str, Any]:
    sev_total: Counter[str] = Counter()
    sev_fixed: Counter[str] = Counter()
    root_c: Counter[str] = Counter()
    base_dqs = 0.0
    valid = 0
    for b in bugs:
        sev = str(b.get("severity") or "").strip() or "（空）"
        sev_total[sev] += 1
        if _is_bug_fixed(str(b.get("status") or "")):
            sev_fixed[sev] += 1
        if b.get("severity"):
            valid += 1
            base_dqs += SEVERITY_W.get(sev, 0.0)
        root = str(b.get("rootCause") or "").strip() or RETRO_RULES["rootCauseEmpty"]
        root_c[root] += 1

    severity_rows = []
    for sev in SEVERITY_ORDER + [k for k in sev_total if k not in SEVERITY_ORDER]:
        t = sev_total[sev]
        f = sev_fixed[sev]
        severity_rows.append(
            {
                "severity": sev,
                "count": t,
                "fixed": f,
                "fixRate": _pct(f, t),
            }
        )
    total = sum(sev_total.values())
    fixed = sum(sev_fixed.values())
    root_rows = [
        {"rootCause": k, "count": v, "share": _pct(v, total)}
        for k, v in root_c.most_common()
    ]
    return {
        "total": total,
        "fixed": fixed,
        "fixedRate": _pct(fixed, total),
        "validBugs": valid,
        "baseDqs": _round(base_dqs),
        "severityRows": severity_rows,
        "rootCauseRows": root_rows,
    }


def _personal_bugs(bugs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    dqs: dict[str, float] = defaultdict(float)
    for b in bugs:
        owner = str(b.get("owner") or "").strip() or "（空）"
        counts[owner] += 1
        sev = str(b.get("severity") or "").strip()
        if sev:
            dqs[owner] += SEVERITY_W.get(sev, 0.0)
    return [
        {"owner": name, "bugCount": n, "baseDqs": _round(dqs[name])}
        for name, n in counts.most_common()
    ]


def _hr_table(
    stories: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    tis: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plan: dict[str, float] = defaultdict(float)
    done: dict[str, float] = defaultdict(float)

    def add_row(row: dict[str, Any], *, role: str) -> None:
        owner = str(row.get("owner") or "").strip()
        pd, pq = item_story_points(row)
        if role == "qc":
            qcs = row.get("qcOwners") or []
            owner = str(qcs[0] if qcs else "").strip()
            pts = pq
        else:
            pts = pd
        if not owner or owner == "（空）":
            return
        if is_lead_person(owner):
            return
        if not pts:
            return
        plan[owner] += pts
        if _is_point_done(str(row.get("status") or "")):
            done[owner] += pts

    for r in stories:
        add_row(r, role="dev")
        if item_story_points(r)[1]:
            add_row(r, role="qc")
    for r in tis:
        add_row(r, role="dev")
        if item_story_points(r)[1]:
            add_row(r, role="qc")
    for r in tasks:
        owner = str(r.get("owner") or "").strip()
        if is_qc_roster_person(owner):
            pd, _pq = item_story_points(r)
            if not owner or owner == "（空）" or is_lead_person(owner) or not pd:
                continue
            plan[owner] += pd
            if _is_point_done(str(r.get("status") or "")):
                done[owner] += pd
        else:
            add_row(r, role="dev")

    names = sorted(plan.keys(), key=lambda n: (-plan[n], n))
    rows = []
    for name in names:
        p = plan[name]
        d = done[name]
        rows.append(
            {
                "owner": name,
                "planPoint": _round(p),
                "donePoint": _round(d),
                "openPoint": _round(max(0.0, p - d)),
            }
        )
    return rows


def _personal_points_by_role(
    stories: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    tis: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """DEV/QC personal rows. QC Tasks (roster Owner) count as QC, not DEV."""
    dev_plan: dict[str, float] = defaultdict(float)
    dev_done: dict[str, float] = defaultdict(float)
    qc_plan: dict[str, float] = defaultdict(float)
    qc_done: dict[str, float] = defaultdict(float)

    def add(
        plan: dict[str, float],
        done: dict[str, float],
        name: str,
        pts: float,
        finished: bool,
        *,
        role: str,
    ) -> None:
        display = _canon_name(name) or str(name or "").strip()
        if not display or display == "（空）" or is_lead_person(display) or is_lead_person(name):
            return
        if not pts:
            return
        forced = PERSON_ROLE_OVERRIDE.get(display) or PERSON_ROLE_OVERRIDE.get(str(name or "").strip())
        if role == "qc" and forced == "DEV":
            return
        if role == "dev" and forced == "QC":
            return
        plan[display] += pts
        if finished:
            done[display] += pts

    def rows_of(plan: dict[str, float], done: dict[str, float]) -> list[dict[str, Any]]:
        names = sorted(plan.keys(), key=lambda n: (-plan[n], n))
        out: list[dict[str, Any]] = []
        for name in names:
            p = plan[name]
            d = done[name]
            out.append(
                {
                    "owner": name,
                    "planPoint": _round(p),
                    "donePoint": _round(d),
                    "openPoint": _round(max(0.0, p - d)),
                }
            )
        return out

    for r in stories + tis:
        pd, pq = item_story_points(r)
        finished = _is_point_done(str(r.get("status") or ""))
        add(dev_plan, dev_done, str(r.get("owner") or ""), pd, finished, role="dev")
        qcs = r.get("qcOwners") or []
        add(qc_plan, qc_done, str(qcs[0] if qcs else ""), pq, finished, role="qc")
    for r in tasks:
        pd, _pq = item_story_points(r)
        finished = _is_point_done(str(r.get("status") or ""))
        owner = str(r.get("owner") or "").strip()
        if is_qc_roster_person(owner):
            add(qc_plan, qc_done, owner, pd, finished, role="qc")
        else:
            add(dev_plan, dev_done, owner, pd, finished, role="dev")
    return rows_of(dev_plan, dev_done), rows_of(qc_plan, qc_done)


def _sprint_recency_key(name: str) -> str:
    dates = re.findall(r"(\d{8})", name or "")
    return max(dates) if dates else ""


def _load_history_summaries(current_sprint: str, limit: int = 3) -> list[dict[str, Any]]:
    """Load prior retro snapshots as compact metrics for -1/-2/-3."""
    files = list_retro_latest_files()
    scored: list[tuple[str, Path, dict[str, Any]]] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        sprint = str(data.get("sprint") or path.name.replace("_latest.json", ""))
        if sprint == current_sprint:
            continue
        key = _sprint_recency_key(str(data.get("feishuSprint") or sprint))
        if not key:
            continue
        scored.append((key, path, data))
    scored.sort(key=lambda x: x[0], reverse=True)

    out: list[dict[str, Any]] = []
    for key, _path, data in scored[:limit]:
        stories = list(data.get("stories") or [])
        tasks = list(data.get("tasks") or [])
        tis = list(data.get("techImprovements") or [])
        bugs = list(data.get("bugs") or [])
        sb = _type_block("User Story", stories)
        tb = _type_block("Task", tasks)
        tib = _type_block("Tech Improvement", tis)
        bug_a = _bug_analysis(bugs)
        plan_total = sb["planTotal"] + tb["planTotal"] + tib["planTotal"]
        done_total = sb["doneTotal"] + tb["doneTotal"] + tib["doneTotal"]
        done_dev = sb["doneDev"] + tb["doneDev"] + tib["doneDev"]
        item_total = sb["total"] + tb["total"] + tib["total"]
        item_done = sb["doneCount"] + tb["doneCount"] + tib["doneCount"]
        norm = (
            _round(bug_a["baseDqs"] / done_dev)
            if done_dev
            else None
        )
        out.append(
            {
                "sprint": data.get("sprint") or "",
                "feishuSprint": data.get("feishuSprint") or "",
                "recencyKey": key,
                "itemDoneRate": _pct(item_done, item_total),
                "pointDoneRate": _pct(done_total, plan_total),
                "bugTotal": bug_a["total"],
                "bugFixedRate": bug_a["fixedRate"],
                "baseDqs": bug_a["baseDqs"],
                "normalizedDqs": norm,
                "doneDevPoint": done_dev,
            }
        )
    return out


def _parse_sprint_window(name: str) -> tuple[str, str]:
    """OBIS-YYYYMMDD-YYYYMMDD → (YYYY-MM-DD, YYYY-MM-DD)."""
    m = re.search(r"(\d{8}).*?(\d{8})", name or "")
    if not m:
        return "", ""

    def fmt(d: str) -> str:
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"

    return fmt(m.group(1)), fmt(m.group(2))


def build_retro_report(*, sprint: str) -> dict[str, Any]:
    sprint = (sprint or "").strip()
    if not sprint:
        raise ValueError("sprint 不能为空")

    snap = load_retro_snapshot(sprint)
    if not snap:
        raise FileNotFoundError(
            f"尚无复盘快照：exports/retro/{sprint}_latest.json，请先刷新"
        )

    ov = load_override(sprint)
    retro = normalize_retro(ov.get("retro"))

    stories = [r for r in (snap.get("stories") or []) if isinstance(r, dict)]
    tasks = [r for r in (snap.get("tasks") or []) if isinstance(r, dict)]
    tis = [r for r in (snap.get("techImprovements") or []) if isinstance(r, dict)]
    bugs = [r for r in (snap.get("bugs") or []) if isinstance(r, dict)]

    story_b = _type_block("User Story", stories)
    task_b = _type_block("Task", tasks)
    ti_b = _type_block("Tech Improvement", tis)
    type_blocks = [story_b, task_b, ti_b]

    plan_total = sum(b["planTotal"] for b in type_blocks)
    done_total = sum(b["doneTotal"] for b in type_blocks)
    plan_dev = sum(b["planDev"] for b in type_blocks)
    done_dev = sum(b["doneDev"] for b in type_blocks)
    item_total = sum(b["total"] for b in type_blocks)
    item_done = sum(b["doneCount"] for b in type_blocks)

    bug_a = _bug_analysis(bugs)
    personal = _personal_bugs(bugs)
    hr = _hr_table(stories, tasks, tis)

    # attach normalized DQS to personal rows using DEV done points from HR
    hr_done = {r["owner"]: float(r["donePoint"]) for r in hr}
    personal_enriched = []
    for row in personal:
        denom = hr_done.get(row["owner"], 0.0)
        personal_enriched.append(
            {
                **row,
                "donePoint": _round(denom),
                "normalizedDqs": (
                    _round(row["baseDqs"] / denom) if denom else None
                ),
            }
        )

    team_norm = (
        _round(bug_a["baseDqs"] / done_dev) if done_dev else None
    )

    plan = load_retro_plan(sprint)
    scope = _scope_diff(plan, snap)
    # merge manual scope change notes
    manual_scope = list(retro.get("scopeChangeNotes") or [])
    reopen_rows = list(retro.get("reopenRows") or [])

    win_start = str(retro.get("sprintWindowStart") or "").strip()
    win_end = str(retro.get("sprintWindowEnd") or "").strip()
    if not win_start and not win_end:
        win_start, win_end = _parse_sprint_window(
            str(snap.get("feishuSprint") or sprint)
        )
    if win_start and win_end:
        sprint_window = f"{win_start} ~ {win_end}"
    else:
        sprint_window = win_start or win_end or ""

    workdays = retro.get("workdays")
    history = _load_history_summaries(sprint, limit=3)
    unfinished = (
        story_b["unfinished"] + task_b["unfinished"] + ti_b["unfinished"]
    )

    report_date = now_beijing_mmdd()
    title = f"Sprint_Retro_Report_{report_date}"

    return {
        "reportType": "retro",
        "title": title,
        "sprint": sprint,
        "feishuSprint": snap.get("feishuSprint") or sprint,
        "sprintResolveNote": snap.get("sprintResolveNote"),
        "fetchedAt": snap.get("fetchedAt"),
        "generatedAt": now_beijing_iso(),
        "timezone": "Asia/Shanghai",
        "sprintWindow": sprint_window,
        "sprintWindowStart": win_start,
        "sprintWindowEnd": win_end,
        "workdays": workdays,
        "membersNote": retro.get("membersNote") or "",
        "pointNote": (
            "Point = Story Point 字段（DEV/QC）；完成 Point = 父 Item 状态为"
            "「已验收/待验收」时计入全部计划 SP（非节点估分）。"
            "Lead 不计入 DEV/QC 估分。"
            "Task Owner 在 QC 名单中时计入 QC。"
            "单条 Story Point > 40 视为异常估分，不计入汇总。"
        ),
        "highlights": retro.get("highlights") or "",
        "concerns": retro.get("concerns") or "",
        "nextFocus": retro.get("nextFocus") or "",
        "overview": {
            "itemTotal": item_total,
            "itemDone": item_done,
            "itemDoneRate": _pct(item_done, item_total),
            "planTotal": _round(plan_total),
            "doneTotal": _round(done_total),
            "pointDoneRate": _pct(done_total, plan_total),
            "planDev": _round(plan_dev),
            "doneDev": _round(done_dev),
            "bugTotal": bug_a["total"],
            "bugFixedRate": bug_a["fixedRate"],
            "baseDqs": bug_a["baseDqs"],
            "normalizedDqs": team_norm,
        },
        "typeBlocks": type_blocks,
        "pointTotals": {
            "planDev": _round(plan_dev),
            "planQc": _round(sum(b["planQc"] for b in type_blocks)),
            "planTotal": _round(plan_total),
            "doneDev": _round(done_dev),
            "doneQc": _round(sum(b["doneQc"] for b in type_blocks)),
            "doneTotal": _round(done_total),
            "pointDoneRate": _pct(done_total, plan_total),
        },
        "unfinishedItems": unfinished,
        "scope": scope,
        "scopeChangeNotes": manual_scope,
        "bugs": bug_a,
        "personalBugs": personal_enriched,
        "hrRows": hr,
        "reopenRows": reopen_rows,
        "history": history,
        "override": {"retro": retro},
    }


def render_retro_html(report: dict[str, Any]) -> str:
    template = env.get_template("retro_report_email.html")
    return template.render(report=report)

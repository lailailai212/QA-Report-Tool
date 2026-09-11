from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path

from .feishu_snapshot import (
    aggregate_bugs,
    enrich_reopen_rows_with_urls,
    load_all_snapshot_bugs,
    load_feishu_snapshot,
    match_feishu_story,
    match_override_story,
    story_status_color,
)
from .ms_client import MeterSphereClient
from .plan_line import evaluate_plan, is_submitted_status, row_is_ready, story_vs_plan, _status_rank
from .override_store import (
    DEFAULT_EXIT_CRITERIA,
    OVERALL_RESULT_OPTIONS,
    empty_completion,
    load_override,
    normalize_completion,
    parse_test_env_tags,
    split_risk_fields,
    compose_risk_block,
    normalize_risk_rows,
    compose_risk_texts_from_rows,
)
from .timeutil import now_beijing, now_beijing_iso, now_beijing_mmdd

TEMPLATES = Path(__file__).resolve().parent / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html", "xml"]),
    auto_reload=True,
    cache_size=0,
)


def _plan_exec_counts(plan: dict[str, Any] | None) -> dict[str, int]:
    p = plan or {}
    design = int(p.get("design") or 0)
    passed = int(p.get("passed") or 0)
    failed = int(p.get("failed") or 0)
    blocked = int(p.get("blocked") or 0)
    if "noRun" in p and p.get("noRun") is not None:
        no_run = int(p.get("noRun") or 0)
    else:
        no_run = max(0, design - passed - failed - blocked)
    return {
        "design": design,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "noRun": no_run,
    }


def _take_matching_plan(
    story_name: str,
    remaining: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Match one MS plan by summary; consume it so each plan maps to at most one Story."""
    hit = match_feishu_story(story_name, remaining)
    if not hit:
        return None
    hid = str(hit.get("id") or "")
    for i, plan in enumerate(remaining):
        if hid and str(plan.get("id") or "") == hid:
            return remaining.pop(i)
        if plan is hit:
            return remaining.pop(i)
    return hit


def _pct(numer: int, denom: int) -> str:
    if denom <= 0:
        return "-"
    return f"{round(numer * 100.0 / denom)}%"


def _story_detail_row(
    *,
    name: str,
    fs: dict[str, Any],
    plan: dict[str, Any] | None,
    stories_ov: dict[str, Any],
) -> dict[str, Any]:
    exe = _plan_exec_counts(plan)
    design = exe["design"]
    passed = exe["passed"]
    failed = exe["failed"]
    blocked = exe["blocked"]
    no_run = exe["noRun"]
    so = match_override_story(name, stories_ov)
    story_status = fs.get("status") or ""
    ready = so["ready"] if "ready" in so else (fs.get("ready") or "")
    if is_submitted_status(story_status):
        ready = "Yes"
    ov_ready_date = str(so.get("readyDate") or "").strip() if "readyDate" in so else ""
    ready_date = ov_ready_date or (fs.get("readyDate") or "")
    ov_comment = str(so.get("comment") or "").strip() if "comment" in so else ""
    comment = ov_comment or (fs.get("comment") or "")
    return {
        "story": name,
        "parentGroupName": (plan or {}).get("parentGroupName"),
        "design": design,
        "caseNum": design,
        "review": None,
        "reviewRate": None,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "noRun": no_run,
        "passRate": _pct(passed, design),
        "executablePassRate": _pct(passed, passed + failed + no_run),
        "storyStatus": story_status,
        "storyStatusColor": story_status_color(story_status),
        "readyForTesting": ready,
        "readyDate": ready_date,
        "readyComment": comment,
        "storyUrl": fs.get("url") or "",
        "readyOverridden": bool(so),
        "expectedReadyDate": fs.get("expectedReadyDate") or "",
        "msMatched": bool(plan),
    }


def _story_status_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    rank = _status_rank(str(row.get("storyStatus") or ""))
    name = str(row.get("story") or "")
    if rank < 0:
        return (1, 0, name)
    return (0, -rank, name)


def _build_story_rows(
    *,
    stories: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    stories_ov: dict[str, Any],
) -> list[dict[str, Any]]:
    """Feishu Sprint stories are the row source; MS execution is matched by summary."""
    remaining = list(plans)
    if stories:
        rows: list[dict[str, Any]] = []
        for fs in stories:
            name = str(fs.get("name") or "").strip()
            if not name:
                continue
            plan = _take_matching_plan(name, remaining)
            rows.append(
                _story_detail_row(
                    name=name,
                    fs=fs,
                    plan=plan,
                    stories_ov=stories_ov,
                )
            )
    else:
        rows = [
            _story_detail_row(
                name=str(p.get("name") or "").strip(),
                fs={},
                plan=p,
                stories_ov=stories_ov,
            )
            for p in plans
            if str(p.get("name") or "").strip()
        ]
    rows.sort(key=_story_status_sort_key)
    return rows


def build_report(
    *,
    mode: Literal["manual", "scheduled"],
    module_id: str | None = None,
    module_name: str | None = None,
    test_env: str = "",
    risk_block: str = "",
    risk_action: str = "",
    client: MeterSphereClient | None = None,
) -> dict[str, Any]:
    ms = client or MeterSphereClient()
    raw = ms.fetch_module_execution(module_id=module_id, module_name=module_name)
    module = raw["module"]
    summary = raw["summary"]
    sprint_name = module.get("name") or module_name or ""

    snapshot = load_feishu_snapshot(sprint_name) if sprint_name else None
    stories = list(snapshot.get("stories") or []) if snapshot else []
    snapshot_bugs = list(snapshot.get("bugs") or []) if snapshot else []
    bugs_agg = aggregate_bugs(snapshot_bugs) if snapshot else None

    ov = load_override(sprint_name) if sprint_name else {}
    stories_ov: dict[str, Any] = ov.get("stories") or {}

    def _override_reopen_rows() -> list[dict[str, Any]]:
        # Match across all local sprint snapshots (reopen bugs may belong to prior Sprint)
        pool = load_all_snapshot_bugs(prefer_sprint=sprint_name) or snapshot_bugs
        return enrich_reopen_rows_with_urls(
            list(ov.get("reopenRows") or []),
            pool,
        )

    # ENV / Risk: scheduled always from override; manual prefers request,
    # falls back to saved override so preview still works if form is empty.
    ov_risk, ov_action = split_risk_fields(ov.get("riskBlock") or "", ov.get("riskAction"))
    ov_rows = normalize_risk_rows(
        ov.get("riskRows"),
        fallback_risk=ov_risk,
        fallback_action=ov_action,
    )
    if mode == "scheduled":
        env_text = ov.get("testEnv") or ""
        risk_rows = ov_rows
        risk_text, action_text = (
            compose_risk_texts_from_rows(risk_rows) if risk_rows else (ov_risk, ov_action)
        )
    else:
        env_text = (test_env or "").strip() or (ov.get("testEnv") or "")
        if ov_rows:
            risk_rows = ov_rows
            risk_text, action_text = compose_risk_texts_from_rows(risk_rows)
        else:
            req_risk, req_action = split_risk_fields(risk_block or "", risk_action)
            risk_text = req_risk or ov_risk
            action_text = req_action or ov_action
            risk_rows = normalize_risk_rows(
                None,
                fallback_risk=risk_text,
                fallback_action=action_text,
            )

    rows = _build_story_rows(
        stories=stories,
        plans=list(raw.get("plans") or []),
        stories_ov=stories_ov,
    )

    total_case = sum(int(r["caseNum"] or 0) for r in rows)
    total_passed = sum(int(r["passed"] or 0) for r in rows)
    total_failed = sum(int(r["failed"] or 0) for r in rows)
    total_blocked = sum(int(r["blocked"] or 0) for r in rows)
    total_no_run = sum(int(r["noRun"] or 0) for r in rows)
    row_totals = {
        "caseNum": total_case,
        "passed": total_passed,
        "failed": total_failed,
        "blocked": total_blocked,
        "noRun": total_no_run,
        "passRate": _pct(total_passed, total_case),
        "executablePassRate": _pct(
            total_passed, total_passed + total_failed + total_no_run
        ),
    }

    if bugs_agg is not None and ov.get("reopenRows") is not None:
        bugs_agg = dict(bugs_agg)
        bugs_agg["reopenRows"] = _override_reopen_rows()
        bugs_agg["reopenSource"] = "override"
    elif bugs_agg is not None:
        bugs_agg = dict(bugs_agg)
        bugs_agg["reopenSource"] = "feishu"
    elif ov.get("reopenRows") is not None:
        # No feishu bugs block, but still show reopen from override via synthetic bugs
        bugs_agg = {
            "total": 0,
            "statusColumns": [],
            "matrixRows": [],
            "matrixTotals": {"counts": {}, "total": 0, "fixedRate": ""},
            "p0p1Rows": [],
            "reopenRows": _override_reopen_rows(),
            "reopenSource": "override",
        }

    pass_rate = summary.get("passRate")
    progress = (
        f"测试用例整体通过率：{pass_rate}%" if pass_rate is not None else "测试用例整体通过率：-"
    )
    if bugs_agg is not None and bugs_agg.get("total"):
        progress = f"{progress} | Bug 总数：{bugs_agg['total']}"
    elif snapshot is not None and bugs_agg is not None:
        progress = f"{progress} | Bug 总数：{bugs_agg.get('total', 0)}"

    report_date = now_beijing_mmdd()
    title = f"Sprint_Daily_Report_{report_date}"
    today = now_beijing().date()
    p0p1_open = 0
    if bugs_agg is not None:
        p0p1_open = len(bugs_agg.get("p0p1Rows") or [])
    plan_eval = evaluate_plan(
        ov.get("plan"),
        sprint=sprint_name,
        rows=rows,
        p0p1_open=p0p1_open,
        today=today,
    )
    reviews = plan_eval.get("reviews") or {}
    for row in rows:
        vs = story_vs_plan(row, today=today, reviews=reviews)
        row["vsPlan"] = vs.get("label") or ""
        row["vsPlanTone"] = vs.get("tone") or ""
        row["vsPlanVerdict"] = vs.get("verdict") or ""
        row["vsNote"] = vs.get("note") or ""
        rv = reviews.get(str(row.get("story") or "")) or {}
        row["reviewResult"] = rv.get("reviewResult") or ""
        row["reviewDate"] = rv.get("reviewDate") or ""

    highlight_rows = [r for r in rows if r.get("vsPlanVerdict") == "behind"]
    highlight_rows.extend(
        r for r in rows if r.get("vsPlanVerdict") != "behind" and int(r.get("failed") or 0) > 0
    )
    seen: set[str] = set()
    slim_rows: list[dict[str, Any]] = []
    for r in highlight_rows:
        key = str(r.get("story") or "")
        if key in seen:
            continue
        seen.add(key)
        slim_rows.append(r)
        if len(slim_rows) >= 5:
            break
    if len(slim_rows) < 5:
        for r in rows:
            key = str(r.get("story") or "")
            if key in seen:
                continue
            slim_rows.append(r)
            if len(slim_rows) >= 5:
                break

    ready_yes = sum(1 for r in rows if row_is_ready(r))
    story_total = len(rows)
    exec_ran = row_totals["passed"] + row_totals["failed"] + row_totals["blocked"]
    exec_pct = (
        round(100.0 * exec_ran / row_totals["caseNum"]) if row_totals["caseNum"] else 0
    )
    risk_override = str(ov.get("riskLevel") or "").strip()
    risk_key = risk_override if risk_override in {"ok", "low", "warn", "danger"} else plan_eval["risk"]
    risk_label = {"ok": "正常", "low": "低风险", "warn": "中风险", "danger": "高风险"}.get(
        risk_key, plan_eval.get("riskLabel") or ""
    )
    conclusion = str(ov.get("dailyConclusion") or "").strip() or plan_eval.get("autoConclusion") or ""
    attention = str(ov.get("attention") or "").strip()
    bugs_hint = ""
    if bugs_agg:
        p0p1 = list(bugs_agg.get("p0p1Rows") or [])
        reopen_n = len(bugs_agg.get("reopenRows") or [])
        if p0p1:
            first = p0p1[0]
            bugs_hint = (
                f"未关 P0/P1：{first.get('priority') or ''} · {first.get('status') or ''} · "
                f"{first.get('summary') or first.get('name') or ''}"
            )
        if reopen_n:
            bugs_hint = (bugs_hint + f"。Reopen {reopen_n}。" if bugs_hint else f"Reopen {reopen_n}。")

    workday = plan_eval.get("workday") or {}
    subtitle = sprint_name
    if workday.get("label"):
        subtitle = f"{sprint_name} · {workday['label']}"

    details_path = f"/report/details?module_name={quote(sprint_name)}"
    if module.get("id"):
        details_path = f"{details_path}&module_id={quote(str(module.get('id')))}"

    return {
        "mode": mode,
        "title": title,
        "moduleName": module.get("name"),
        "moduleId": module.get("id"),
        "testEnv": env_text,
        "testEnvTags": parse_test_env_tags(env_text),
        "riskBlock": risk_text,
        "riskAction": action_text,
        "riskRows": risk_rows,
        "dailyConclusion": conclusion,
        "attention": attention,
        "riskLevel": risk_key,
        "riskLabel": risk_label,
        "testingProgress": progress,
        "summary": summary,
        "rows": rows,
        "rowTotals": row_totals,
        "slimRows": slim_rows,
        "slimHidden": max(0, len(rows) - len(slim_rows)),
        "kpis": {
            "readyYes": ready_yes,
            "storyTotal": story_total,
            "execPct": exec_pct,
            "execRan": exec_ran,
            "passRate": row_totals.get("executablePassRate") or row_totals.get("passRate") or "-",
            "bugTotal": (bugs_agg or {}).get("total") or 0,
            "bugOpen": len((bugs_agg or {}).get("openRows") or []),
            "bugFixedRate": ((bugs_agg or {}).get("matrixTotals") or {}).get("fixedRate") or "",
            "p0p1Open": p0p1_open,
        },
        "bugsHint": bugs_hint,
        "plan": plan_eval,
        "subtitle": subtitle,
        "detailsPath": details_path,
        "override": {
            "loaded": bool(ov.get("updatedAt")),
            "updatedAt": ov.get("updatedAt"),
            "storyCount": len(stories_ov),
            "reopenManual": ov.get("reopenRows") is not None,
            "reopenCount": len(ov.get("reopenRows") or [])
            if ov.get("reopenRows") is not None
            else None,
            "testEnv": ov.get("testEnv") or "",
            "riskBlock": ov.get("riskBlock") or "",
            "riskAction": ov.get("riskAction") or "",
            "riskRows": ov.get("riskRows") or [],
            "dailyConclusion": ov.get("dailyConclusion") or "",
            "attention": ov.get("attention") or "",
            "riskLevel": ov.get("riskLevel") or "",
            "plan": ov.get("plan") or {},
        },
        "feishu": {
            "loaded": snapshot is not None,
            "fetchedAt": (snapshot or {}).get("fetchedAt"),
            "bugs": bugs_agg,
            "bugsList": [
                {
                    "id": b.get("id"),
                    "summary": (b.get("summary") or b.get("name") or ""),
                    "name": (b.get("name") or b.get("summary") or ""),
                    "url": b.get("url") or "",
                    "priority": b.get("priority") or "",
                    "status": b.get("status") or "",
                }
                for b in snapshot_bugs
            ],
            "warning": (
                None
                if snapshot
                else f"未找到飞书快照 exports/feishu/{sprint_name}_latest.json"
            ),
        },
        "generatedAt": now_beijing_iso(),
        "timezone": "Asia/Shanghai",
    }


def render_html(report: dict[str, Any]) -> str:
    # Ensure tags exist even if caller only provided testEnv text
    payload = dict(report)
    tags = payload.get("testEnvTags")
    if not tags:
        payload["testEnvTags"] = parse_test_env_tags(payload.get("testEnv"))
    risk, action = split_risk_fields(payload.get("riskBlock"), payload.get("riskAction"))
    payload["riskBlock"] = risk
    payload["riskAction"] = action
    template = env.get_template("daily_report_email.html")
    return template.render(report=payload)


def render_details_html(report: dict[str, Any]) -> str:
    payload = dict(report)
    tags = payload.get("testEnvTags")
    if not tags:
        payload["testEnvTags"] = parse_test_env_tags(payload.get("testEnv"))
    risk, action = split_risk_fields(payload.get("riskBlock"), payload.get("riskAction"))
    payload["riskBlock"] = risk
    payload["riskAction"] = action
    template = env.get_template("daily_report_details.html")
    return template.render(report=payload)


def _parse_pct(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return None


def _is_ready_delay(comment: str) -> bool:
    return "提测delay" in (comment or "").casefold()


def _suggest_overall_result(
    *,
    exec_pass_rate: float | None,
    open_p0: int,
    open_p1: int,
    open_bugs: int,
    risk_text: str,
    criteria: dict[str, Any],
) -> str:
    exec_min = int(criteria.get("execPassMin") or DEFAULT_EXIT_CRITERIA["execPassMin"])
    p0p1_max = int(criteria.get("p0p1OpenMax") or DEFAULT_EXIT_CRITERIA["p0p1OpenMax"])
    open_p0p1 = open_p0 + open_p1
    fail = False
    if exec_pass_rate is None or exec_pass_rate < exec_min:
        fail = True
    if open_p0p1 > p0p1_max:
        fail = True
    if fail:
        return "Fail"
    if open_bugs > 0 or bool((risk_text or "").strip()):
        return "Pass with Risk"
    return "Pass"


def _default_summary_one_liner(report_bits: dict[str, Any]) -> str:
    story_total = report_bits.get("storyTotal", 0)
    story_done = report_bits.get("storyDone", 0)
    exec_pass = report_bits.get("executablePassRate") or "-"
    bug_total = report_bits.get("bugTotal", 0)
    fixed_rate = report_bits.get("fixedRate") or "-"
    open_p0 = report_bits.get("openP0", 0)
    open_p1 = report_bits.get("openP1", 0)
    open_bugs = report_bits.get("openBugs", 0)
    result = report_bits.get("overallResult") or "-"
    return (
        f"本 Sprint 共 {story_total} Story（完成/关闭 {story_done}），"
        f"Executable Pass Rate {exec_pass}，Bug {bug_total}（Fixed Rate {fixed_rate}），"
        f"未关闭 P0/P1 = {open_p0}/{open_p1}，遗留未关闭 Bug {open_bugs}。"
        f"结项结论：{result}。"
    )


def build_completion_report(
    *,
    module_id: str | None = None,
    module_name: str | None = None,
    test_env: str = "",
    risk_block: str = "",
    client: MeterSphereClient | None = None,
) -> dict[str, Any]:
    """Sprint QA completion report (preview/send only; no scheduler)."""
    base = build_report(
        mode="manual",
        module_id=module_id,
        module_name=module_name,
        test_env=test_env,
        risk_block=risk_block,
        client=client,
    )
    sprint_name = base.get("moduleName") or module_name or ""
    ov = load_override(sprint_name) if sprint_name else {}
    completion = normalize_completion(ov.get("completion"))
    criteria = completion.get("exitCriteria") or dict(DEFAULT_EXIT_CRITERIA)

    snapshot = load_feishu_snapshot(sprint_name) if sprint_name else None
    snapshot_stories = list((snapshot or {}).get("stories") or [])

    # Story rows: keep Delay mark only (drop Ready Date / Comment columns)
    story_rows = []
    delay_count = 0
    for row in base.get("rows") or []:
        delay = _is_ready_delay(str(row.get("readyComment") or ""))
        if delay:
            delay_count += 1
        story_rows.append(
            {
                "story": row.get("story"),
                "storyUrl": row.get("storyUrl") or "",
                "storyStatus": row.get("storyStatus") or "",
                "storyStatusColor": row.get("storyStatusColor") or "",
                "caseNum": row.get("caseNum"),
                "passed": row.get("passed"),
                "failed": row.get("failed"),
                "blocked": row.get("blocked"),
                "noRun": row.get("noRun"),
                "passRate": row.get("passRate"),
                "executablePassRate": row.get("executablePassRate"),
                "readyDelay": "Yes" if delay else "No",
            }
        )

    # Prefer full Feishu story list for status distribution / delay count
    status_dist: dict[str, int] = {}
    story_done = 0
    if snapshot_stories:
        delay_count = 0
        for s in snapshot_stories:
            st = str(s.get("status") or "").strip() or "（空）"
            status_dist[st] = status_dist.get(st, 0) + 1
            if st in {"已完成", "已关闭"}:
                story_done += 1
            if _is_ready_delay(str(s.get("comment") or "")):
                delay_count += 1
        story_total = len(snapshot_stories)
    else:
        for row in story_rows:
            st = str(row.get("storyStatus") or "").strip() or "（空）"
            status_dist[st] = status_dist.get(st, 0) + 1
            if st in {"已完成", "已关闭"}:
                story_done += 1
        story_total = len(story_rows)

    status_dist_rows = [
        {"status": k, "count": v}
        for k, v in sorted(status_dist.items(), key=lambda x: (-x[1], x[0]))
    ]

    bugs_agg = (base.get("feishu") or {}).get("bugs") or {}
    open_rows_raw = list(bugs_agg.get("openRows") or [])
    notes = completion.get("openBugNotes") or {}
    open_rows = []
    open_p0 = open_p1 = 0
    for b in open_rows_raw:
        pri = str(b.get("priority") or "").upper()
        if pri == "P0":
            open_p0 += 1
        elif pri == "P1":
            open_p1 += 1
        bug_id = str(b.get("id") or "").strip()
        summary = str(b.get("summary") or b.get("name") or "").strip()
        note = ""
        if bug_id and bug_id in notes:
            note = notes[bug_id]
        elif summary and summary in notes:
            note = notes[summary]
        open_rows.append(
            {
                "id": bug_id,
                "priority": b.get("priority") or "",
                "status": b.get("status") or "",
                "summary": summary,
                "url": b.get("url") or "",
                "impact": note,
            }
        )

    row_totals = base.get("rowTotals") or {}
    exec_pass = row_totals.get("executablePassRate")
    exec_pass_num = _parse_pct(exec_pass)
    fixed_rate = (bugs_agg.get("matrixTotals") or {}).get("fixedRate") or ""
    bug_total = int(bugs_agg.get("total") or 0)
    reopen_rows = list(bugs_agg.get("reopenRows") or [])
    reopen_count = len(reopen_rows)

    suggested = _suggest_overall_result(
        exec_pass_rate=exec_pass_num,
        open_p0=open_p0,
        open_p1=open_p1,
        open_bugs=len(open_rows),
        risk_text=compose_risk_block(
            base.get("riskBlock") or "",
            base.get("riskAction") or "",
        ),
        criteria=criteria,
    )
    manual_result = str(completion.get("overallResult") or "").strip()
    if manual_result in OVERALL_RESULT_OPTIONS:
        overall_result = manual_result
        overall_source = "manual"
    else:
        overall_result = suggested
        overall_source = "auto"

    summary_bits = {
        "storyTotal": story_total,
        "storyDone": story_done,
        "executablePassRate": exec_pass or "-",
        "bugTotal": bug_total,
        "fixedRate": fixed_rate or "-",
        "openP0": open_p0,
        "openP1": open_p1,
        "openBugs": len(open_rows),
        "overallResult": overall_result,
    }
    auto_one_liner = _default_summary_one_liner(summary_bits)
    one_liner = str(completion.get("summaryOneLiner") or "").strip() or auto_one_liner

    exit_text = (
        f"Executable Pass Rate ≥ {criteria.get('execPassMin', 95)}%；"
        f"未关闭 P0/P1 ≤ {criteria.get('p0p1OpenMax', 0)}"
    )

    window_start = completion.get("testWindowStart") or ""
    window_end = completion.get("testWindowEnd") or ""
    if window_start and window_end:
        test_window = f"{window_start} ~ {window_end}"
    elif window_start or window_end:
        test_window = window_start or window_end
    else:
        test_window = ""

    overall_colors = {
        "Pass": "#059669",
        "Pass with Risk": "#D97706",
        "Fail": "#DC2626",
    }

    report_date = now_beijing_mmdd()
    title = f"Sprint_QA_Completion_Report_{report_date}"

    return {
        "mode": "manual",
        "reportType": "completion",
        "title": title,
        "moduleName": base.get("moduleName"),
        "moduleId": base.get("moduleId"),
        "testEnv": base.get("testEnv"),
        "testEnvTags": base.get("testEnvTags") or [],
        "riskBlock": compose_risk_block(
            base.get("riskBlock") or "",
            base.get("riskAction") or "",
        ),
        "testOwner": completion.get("testOwner") or "",
        "testWindow": test_window,
        "testWindowStart": window_start,
        "testWindowEnd": window_end,
        "overallResult": overall_result,
        "overallResultSuggested": suggested,
        "overallResultSource": overall_source,
        "overallResultColor": overall_colors.get(overall_result, "#1E4A7A"),
        "overallResultOptions": list(OVERALL_RESULT_OPTIONS),
        "exitCriteria": criteria,
        "exitCriteriaText": exit_text,
        "summaryOneLiner": one_liner,
        "summaryOneLinerAuto": auto_one_liner,
        "deferredItems": completion.get("deferredItems") or "",
        "recommendations": completion.get("recommendations") or "",
        "signOff": completion.get("signOff") or empty_completion()["signOff"],
        "qualitySnapshot": {
            "storyTotal": story_total,
            "storyDone": story_done,
            "storyOpen": max(0, story_total - story_done),
            "readyDelayCount": delay_count,
            "caseTotal": row_totals.get("caseNum", 0),
            "passed": row_totals.get("passed", 0),
            "failed": row_totals.get("failed", 0),
            "blocked": row_totals.get("blocked", 0),
            "noRun": row_totals.get("noRun", 0),
            "passRate": row_totals.get("passRate") or "-",
            "executablePassRate": exec_pass or "-",
            "bugTotal": bug_total,
            "fixedRate": fixed_rate or "-",
            "openP0": open_p0,
            "openP1": open_p1,
            "openBugs": len(open_rows),
            "reopenCount": reopen_count,
        },
        "storyStatusDist": status_dist_rows,
        "rows": story_rows,
        "rowTotals": row_totals,
        "openBugs": open_rows,
        "feishu": base.get("feishu"),
        "override": {
            **(base.get("override") or {}),
            "completion": completion,
        },
        "generatedAt": now_beijing_iso(),
        "timezone": "Asia/Shanghai",
    }


def render_completion_html(report: dict[str, Any]) -> str:
    payload = dict(report)
    tags = payload.get("testEnvTags")
    if not tags:
        payload["testEnvTags"] = parse_test_env_tags(payload.get("testEnv"))
    template = env.get_template("completion_report_email.html")
    return template.render(report=payload)

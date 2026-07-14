from __future__ import annotations

from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path

from .feishu_snapshot import aggregate_bugs, load_feishu_snapshot, story_index
from .ms_client import MeterSphereClient
from .override_store import load_override
from .timeutil import now_beijing_iso, now_beijing_mmdd

TEMPLATES = Path(__file__).resolve().parent / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html", "xml"]),
    auto_reload=True,
    cache_size=0,
)


def _pct(numer: int, denom: int) -> str | None:
    if denom <= 0:
        return None
    return f"{round(numer * 100.0 / denom)}%"


def build_report(
    *,
    mode: Literal["manual", "scheduled"],
    module_id: str | None = None,
    module_name: str | None = None,
    test_env: str = "",
    risk_block: str = "",
    client: MeterSphereClient | None = None,
) -> dict[str, Any]:
    ms = client or MeterSphereClient()
    raw = ms.fetch_module_execution(module_id=module_id, module_name=module_name)
    module = raw["module"]
    summary = raw["summary"]
    sprint_name = module.get("name") or module_name or ""

    snapshot = load_feishu_snapshot(sprint_name) if sprint_name else None
    by_story = story_index(snapshot) if snapshot else {}
    bugs_agg = aggregate_bugs(list(snapshot.get("bugs") or [])) if snapshot else None

    ov = load_override(sprint_name) if sprint_name else {}
    stories_ov: dict[str, Any] = ov.get("stories") or {}

    # ENV / Risk: scheduled always from override; manual uses request form
    # (form is hydrated from override on the page).
    if mode == "scheduled":
        env_text = ov.get("testEnv") or ""
        risk_text = ov.get("riskBlock") or ""
    else:
        env_text = test_env or ""
        risk_text = risk_block or ""

    rows = []
    for p in raw["plans"]:
        design = int(p["design"] or 0)
        passed = int(p["passed"] or 0)
        failed = int(p["failed"] or 0)
        blocked = int(p["blocked"] or 0)
        # Prefer MS pendingCount (未执行); fall back to residual only if missing
        if "noRun" in p and p["noRun"] is not None:
            no_run = int(p["noRun"] or 0)
        else:
            no_run = max(0, design - passed - failed - blocked)
        name = p["name"]
        fs = by_story.get(name) or {}
        so = stories_ov.get(name) or {}

        ready = so["ready"] if "ready" in so else (fs.get("ready") or "")
        ready_date = so["readyDate"] if "readyDate" in so else (fs.get("readyDate") or "")
        comment = so["comment"] if "comment" in so else (fs.get("comment") or "")

        rows.append(
            {
                "story": name,
                "parentGroupName": p.get("parentGroupName"),
                "design": design,
                "caseNum": design,
                "review": None,
                "reviewRate": None,
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "noRun": no_run,
                "passRate": _pct(passed, design),
                "executablePassRate": _pct(passed, passed + failed),
                "storyStatus": fs.get("status") or "",
                "readyForTesting": ready,
                "readyDate": ready_date,
                "readyComment": comment,
                "storyUrl": fs.get("url") or "",
                "readyOverridden": bool(so),
            }
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
        "executablePassRate": _pct(total_passed, total_passed + total_failed),
    }

    if bugs_agg is not None and ov.get("reopenRows") is not None:
        bugs_agg = dict(bugs_agg)
        bugs_agg["reopenRows"] = list(ov.get("reopenRows") or [])
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
            "reopenRows": list(ov.get("reopenRows") or []),
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
    return {
        "mode": mode,
        "title": title,
        "moduleName": module.get("name"),
        "moduleId": module.get("id"),
        "testEnv": env_text,
        "riskBlock": risk_text,
        "testingProgress": progress,
        "summary": summary,
        "rows": rows,
        "rowTotals": row_totals,
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
        },
        "feishu": {
            "loaded": snapshot is not None,
            "fetchedAt": (snapshot or {}).get("fetchedAt"),
            "bugs": bugs_agg,
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
    template = env.get_template("daily_report_email.html")
    return template.render(report=report)

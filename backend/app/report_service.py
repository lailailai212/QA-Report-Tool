from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path

from .feishu_snapshot import aggregate_bugs, load_feishu_snapshot, story_index
from .ms_client import MeterSphereClient

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

    rows = []
    for p in raw["plans"]:
        design = int(p["design"] or 0)
        passed = int(p["passed"] or 0)
        failed = int(p["failed"] or 0)
        blocked = int(p["blocked"] or 0)
        name = p["name"]
        fs = by_story.get(name) or {}
        rows.append(
            {
                "story": name,
                "parentGroupName": p.get("parentGroupName"),
                "design": design,
                "review": None,
                "reviewRate": None,
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "passRate": _pct(passed, design),
                "executablePassRate": _pct(passed, passed + failed),
                "storyStatus": fs.get("status") or "",
                "readyForTesting": fs.get("ready") or "",
                "readyDate": fs.get("readyDate") or "",
                "readyComment": fs.get("comment") or "",
                "storyUrl": fs.get("url") or "",
            }
        )

    env_text = "" if mode == "scheduled" else (test_env or "")
    risk_text = "" if mode == "scheduled" else (risk_block or "")
    pass_rate = summary.get("passRate")
    progress = (
        f"测试用例整体通过率：{pass_rate}%" if pass_rate is not None else "测试用例整体通过率：-"
    )
    if bugs_agg is not None:
        progress = f"{progress} | Bug 总数：{bugs_agg['total']}"

    report_date = datetime.now().strftime("%m/%d")
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
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
    }


def render_html(report: dict[str, Any]) -> str:
    template = env.get_template("daily_report_email.html")
    return template.render(report=report)

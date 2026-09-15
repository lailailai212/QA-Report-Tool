from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .timeutil import now_beijing

GATE_TYPES = (
    "status_min",
    "ready_count",
    "exec_pct",
    "case_ready",
    "review",
    "accept",
)

GATE_TYPE_LABEL = {
    "status_min": "Story 状态达到",
    "ready_count": "Ready 条数",
    "exec_pct": "用例执行进度",
    "case_ready": "已有用例的 Story",
    "review": "评审通过",
    "accept": "验收完成",
}

STORY_STATUS_RANK: tuple[str, ...] = (
    "待排期",
    "待产品设计评审",
    "待技术评审",
    "产品设计中",
    "技术设计中",
    "开发中",
    "联调中",
    "待提测",
    "待测试",
    "测试中",
    "待验收",
    "已验收",
    "待闭环",
    "已完成",
    "已关闭",
)

# Story 行 vs 计划：已设计划线时，靠后的环节优先作为落后原因
_PHASE_BEHIND_PRIORITY = (
    "accept",
    "exec",
    "ready",
    "case_review",
    "case_design",
    "feature_design",
)

ACCEPT_DONE_STATUSES = frozenset({"已完成", "已关闭"})
READY_YES = frozenset({"yes", "y", "是", "true", "1"})
READY_STATUS_MIN = "待测试"

DEFAULT_PHASES: tuple[dict[str, Any], ...] = (
    {
        "id": "feature_design",
        "name": "功能设计",
        "gateType": "status_min",
        "gateStatus": "开发中",
        "targetPct": 100,
        "span": (0.0, 0.22),
    },
    {
        "id": "case_design",
        "name": "用例设计",
        "gateType": "case_ready",
        "gateStatus": "",
        "targetPct": 100,
        "span": (0.0, 0.28),
    },
    {
        "id": "case_review",
        "name": "用例评审",
        "gateType": "review",
        "gateStatus": "",
        "targetPct": 100,
        "span": (0.12, 0.36),
    },
    {
        "id": "ready",
        "name": "开发提测",
        "gateType": "ready_count",
        "gateStatus": "",
        "targetPct": 100,
        "span": (0.08, 0.72),
    },
    {
        "id": "exec",
        "name": "用例执行",
        "gateType": "exec_pct",
        "gateStatus": "",
        "targetPct": 100,
        "span": (0.22, 0.86),
    },
    {
        "id": "accept",
        "name": "验收收口",
        "gateType": "accept",
        "gateStatus": "",
        "targetPct": 100,
        "span": (0.78, 1.0),
    },
)

COMPARE_PHASE_IDS = ("ready", "exec", "accept")

RISK_LEVELS = ("ok", "low", "warn", "danger")
RISK_LABEL = {"ok": "正常", "low": "低风险", "warn": "中风险", "danger": "高风险"}
VERDICT_LABEL = {
    "not_started": "未开始",
    "on_track": "按计划",
    "ahead": "Ahead",
    "behind": "Behind",
    "unknown": "未维护",
    "no_dates": "未设区间",
}


def parse_iso_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace("/", "-").replace(".", "-")
    if re.fullmatch(r"\d{8}", raw):
        raw = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    try:
        parts = raw[:10].split("-")
        if len(parts) != 3:
            return None
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (TypeError, ValueError):
        return None


def parse_sprint_window(name: str) -> tuple[date | None, date | None]:
    found = re.findall(r"(\d{8})", name or "")
    if len(found) < 2:
        return None, None
    return parse_iso_date(found[0]), parse_iso_date(found[1])


def workdays(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    out: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def default_ready_deadline(sprint: str) -> date | None:
    """默认提测截止日期：Sprint 第二周第一个工作日（开始日 + 7 天后的首个工作日）。"""
    start, end = parse_sprint_window(sprint)
    if not start:
        return None
    week2 = start + timedelta(days=7)
    last = end if end and end >= week2 else week2 + timedelta(days=6)
    days = workdays(week2, last)
    return days[0] if days else None


def fmt_mmdd(value: date | None) -> str:
    if not value:
        return ""
    return value.strftime("%m-%d")


def empty_phase(spec: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = spec or {}
    gate = str(spec.get("gateType") or "status_min").strip()
    if gate not in GATE_TYPES:
        gate = "status_min"
    target_pct = 100
    try:
        target_pct = int(spec.get("targetPct") if spec.get("targetPct") not in (None, "") else 100)
    except (TypeError, ValueError):
        target_pct = 100
    target_pct = max(1, min(100, target_pct))
    target_count = spec.get("targetCount")
    try:
        target_count = int(target_count) if target_count not in (None, "") else None
    except (TypeError, ValueError):
        target_count = None
    if target_count is not None and target_count < 0:
        target_count = None
    enabled = spec.get("enabled")
    if enabled is None:
        enabled = True
    return {
        "id": str(spec.get("id") or "").strip(),
        "name": str(spec.get("name") or "").strip() or "未命名环节",
        "enabled": bool(enabled),
        "start": str(spec.get("start") or "").strip(),
        "end": str(spec.get("end") or "").strip(),
        "gateType": gate,
        "gateStatus": str(spec.get("gateStatus") or "").strip(),
        "targetPct": target_pct,
        "targetCount": target_count,
        "note": str(spec.get("note") or "").strip(),
    }


def default_phase_templates() -> list[dict[str, Any]]:
    return [
        empty_phase(
            {
                "id": row["id"],
                "name": row["name"],
                "gateType": row["gateType"],
                "gateStatus": row["gateStatus"],
                "targetPct": row["targetPct"],
                "enabled": True,
            }
        )
        for row in DEFAULT_PHASES
    ]


def empty_plan() -> dict[str, Any]:
    return {"phases": default_phase_templates(), "reviews": {}}


def normalize_reviews(raw: Any) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, row in raw.items():
        name = str(key or "").strip()
        if not name or not isinstance(row, dict):
            continue
        result = str(row.get("reviewResult") or "").strip()
        review_date = str(row.get("reviewDate") or "").strip()
        if not result and not review_date:
            continue
        out[name] = {"reviewResult": result, "reviewDate": review_date}
    return out


def normalize_plan(raw: Any) -> dict[str, Any]:
    base = empty_plan()
    if not isinstance(raw, dict):
        return base
    saved_rows = raw.get("phases") if isinstance(raw.get("phases"), list) else []
    by_id: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for row in saved_rows:
        if not isinstance(row, dict):
            continue
        phase = empty_phase(row)
        if not phase["id"]:
            phase["id"] = f"custom_{len(extras) + 1}"
        if any(p["id"] == phase["id"] for p in DEFAULT_PHASES):
            by_id[phase["id"]] = phase
        else:
            extras.append(phase)
    phases: list[dict[str, Any]] = []
    for tmpl in default_phase_templates():
        phases.append(by_id.get(tmpl["id"]) or tmpl)
    phases.extend(extras)
    base["phases"] = phases
    base["reviews"] = normalize_reviews(raw.get("reviews"))
    return base


def suggest_phase_dates(sprint: str, phases: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    start, end = parse_sprint_window(sprint)
    rows = [empty_phase(p) for p in (phases or default_phase_templates())]
    if not start or not end:
        return rows
    days = workdays(start, end)
    if not days:
        return rows
    last = len(days) - 1
    spans = {p["id"]: p["span"] for p in DEFAULT_PHASES}
    for row in rows:
        span = spans.get(row["id"])
        if not span:
            continue
        a = max(0, min(last, int(round(span[0] * last))))
        b = max(a, min(last, int(round(span[1] * last))))
        row["start"] = days[a].isoformat()
        row["end"] = days[b].isoformat()
    return rows


def _status_rank(status: str) -> int:
    key = (status or "").strip()
    if key in STORY_STATUS_RANK:
        return STORY_STATUS_RANK.index(key)
    return -1


def _is_ready(value: Any) -> bool:
    return str(value or "").strip().casefold() in READY_YES


def is_submitted_status(status: Any) -> bool:
    """飞书状态已过提测门槛（待测试及之后，含已验收 / 待闭环）。"""
    rank = _status_rank(str(status or ""))
    try:
        min_rank = STORY_STATUS_RANK.index(READY_STATUS_MIN)
    except ValueError:
        return False
    return rank >= min_rank


def row_is_ready(row: dict[str, Any]) -> bool:
    """已提测：飞书状态已过提测门槛，或 Ready 标记为 Yes。"""
    if is_submitted_status(row.get("storyStatus")):
        return True
    return _is_ready(row.get("readyForTesting"))


def _is_review_pass(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text in {"通过", "Pass", "pass", "Y", "Yes", "yes"} or "通过" in text


def _window_progress(start: date | None, end: date | None, today: date) -> dict[str, Any]:
    if not start or not end:
        return {
            "state": "no_dates",
            "ratio": 0.0,
            "elapsed": 0,
            "total": 0,
            "daysBehindUnit": 0.0,
        }
    if end < start:
        start, end = end, start
    days = workdays(start, end)
    total = len(days) or 1
    if today < start:
        return {
            "state": "not_started",
            "ratio": 0.0,
            "elapsed": 0,
            "total": total,
            "daysBehindUnit": 1.0 / total,
        }
    if today > end:
        return {
            "state": "overdue",
            "ratio": 1.0,
            "elapsed": total,
            "total": total,
            "daysBehindUnit": 1.0 / total,
        }
    elapsed = 0
    for d in days:
        elapsed += 1
        if d >= today:
            break
    if not days:
        elapsed = 1
    ratio = min(1.0, elapsed / total)
    state = "due" if today == end else "in_window"
    return {
        "state": state,
        "ratio": ratio,
        "elapsed": elapsed,
        "total": total,
        "daysBehindUnit": 1.0 / total,
    }


def _round_value(value: float, *, pct: bool) -> float:
    if pct:
        return round(value, 1)
    return round(value)


def _measure_actual(
    phase: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    reviews: dict[str, dict[str, str]],
    p0p1_open: int,
) -> dict[str, Any]:
    gate = phase["gateType"]
    n = len(rows)
    if gate == "exec_pct":
        design = sum(int(r.get("caseNum") or 0) for r in rows)
        ran = sum(
            int(r.get("passed") or 0) + int(r.get("failed") or 0) + int(r.get("blocked") or 0)
            for r in rows
        )
        actual = (100.0 * ran / design) if design else 0.0
        target = float(phase.get("targetCount") or phase["targetPct"] or 100)
        return {
            "actual": actual,
            "target": target,
            "pct": True,
            "unit": "pp",
            "unconfigured": False,
            "extra": {"ran": ran, "design": design},
        }

    if gate == "ready_count":
        actual = float(sum(1 for r in rows if row_is_ready(r)))
        target = float(phase["targetCount"] if phase.get("targetCount") is not None else n)
        return {
            "actual": actual,
            "target": target or 0.0,
            "pct": False,
            "unit": "条",
            "unconfigured": n == 0,
            "extra": {"total": n},
        }

    if gate == "case_ready":
        actual = float(sum(1 for r in rows if int(r.get("caseNum") or 0) > 0))
        target = float(phase["targetCount"] if phase.get("targetCount") is not None else n)
        return {
            "actual": actual,
            "target": target or 0.0,
            "pct": False,
            "unit": "条",
            "unconfigured": n == 0,
            "extra": {"total": n},
        }

    if gate == "review":
        actual = 0.0
        configured = 0
        for r in rows:
            name = str(r.get("story") or "")
            rv = reviews.get(name) or {}
            result = rv.get("reviewResult") or r.get("reviewResult") or ""
            if str(result or rv.get("reviewDate") or "").strip():
                configured += 1
            if _is_review_pass(result):
                actual += 1
        target = float(phase["targetCount"] if phase.get("targetCount") is not None else n)
        return {
            "actual": actual,
            "target": target or 0.0,
            "pct": False,
            "unit": "条",
            "unconfigured": n > 0 and configured == 0,
            "extra": {"total": n, "configured": configured},
        }

    if gate == "accept":
        actual = float(
            sum(1 for r in rows if (r.get("storyStatus") or "").strip() in ACCEPT_DONE_STATUSES)
        )
        target = float(phase["targetCount"] if phase.get("targetCount") is not None else n)
        return {
            "actual": actual,
            "target": target or 0.0,
            "pct": False,
            "unit": "条",
            "unconfigured": n == 0,
            "extra": {"total": n, "p0p1Open": p0p1_open},
        }

    # status_min
    gate_status = phase.get("gateStatus") or "开发中"
    need_rank = _status_rank(gate_status)
    actual = 0.0
    known = 0
    for r in rows:
        rank = _status_rank(str(r.get("storyStatus") or ""))
        if rank < 0:
            continue
        known += 1
        if need_rank < 0 or rank >= need_rank:
            actual += 1
    target = float(phase["targetCount"] if phase.get("targetCount") is not None else n)
    return {
        "actual": actual,
        "target": target or 0.0,
        "pct": False,
        "unit": "条",
        "unconfigured": n == 0,
        "extra": {"total": n, "known": known, "gateStatus": gate_status},
    }


def _label_number(value: float, *, pct: bool, unit: str) -> str:
    if pct:
        shown = int(round(value)) if abs(value - round(value)) < 0.05 else round(value, 1)
        return f"{shown}%"
    shown = int(round(value))
    return f"{shown} {unit}".strip()


def evaluate_phase(
    phase: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    today: date,
    reviews: dict[str, dict[str, str]],
    p0p1_open: int,
) -> dict[str, Any]:
    out = empty_phase(phase)
    start = parse_iso_date(out["start"])
    end = parse_iso_date(out["end"])
    out["startLabel"] = fmt_mmdd(start)
    out["endLabel"] = fmt_mmdd(end)
    out["rangeLabel"] = (
        f"{out['startLabel']} ~ {out['endLabel']}" if start and end else "未设区间"
    )
    progress = _window_progress(start, end, today)
    measure = _measure_actual(out, rows, reviews=reviews, p0p1_open=p0p1_open)
    target = float(measure["target"] or 0)
    if not measure["pct"] and out["targetPct"] != 100 and out.get("targetCount") is None and target:
        target = target * (out["targetPct"] / 100.0)
    planned = target * float(progress["ratio"])
    actual = float(measure["actual"] or 0)
    pct = bool(measure["pct"])
    unit = str(measure["unit"] or "")
    extra = dict(measure.get("extra") or {})

    verdict = "on_track"
    tone = "ok"
    unconfigured = bool(measure.get("unconfigured"))
    if not out["enabled"]:
        verdict = "unknown"
        tone = ""
    elif progress["state"] == "no_dates":
        verdict = "no_dates"
        tone = ""
    elif unconfigured:
        verdict = "unknown"
        tone = ""
    elif progress["state"] == "not_started":
        verdict = "not_started"
        tone = "ok"
    else:
        slack = 0.6 if not pct else 0.8
        if actual + slack < planned:
            verdict = "behind"
            tone = "danger" if progress["state"] in {"due", "overdue"} else "warn"
        elif planned > 0 and actual > planned * 1.05 + slack:
            verdict = "ahead"
            tone = "ok"
        else:
            verdict = "on_track"
            tone = "ok"

    if out["enabled"] and out["gateType"] == "accept" and p0p1_open > 0:
        extra["p0p1Open"] = p0p1_open
        if progress["state"] in {"in_window", "due", "overdue"}:
            if verdict != "behind":
                verdict = "behind"
            tone = "danger"

    delta = actual - planned
    days_behind = 0.0
    unit_ratio = float(progress.get("daysBehindUnit") or 0)
    if verdict == "behind" and unit_ratio > 0 and target > 0:
        days_behind = max(0.0, (planned - actual) / (target * unit_ratio))

    if verdict == "behind":
        if pct:
            gap = f"慢了 {int(round(abs(delta)))}pp"
        else:
            gap = f"慢了 {int(round(abs(delta)))} {unit}".strip()
        if days_behind >= 0.5:
            gap = f"{gap} · 约 {int(round(days_behind))} 天"
        verdict_label = gap
    elif verdict == "ahead":
        verdict_label = "Ahead"
    elif verdict == "not_started":
        verdict_label = "未开始"
    elif verdict == "unknown":
        verdict_label = "未维护"
    elif verdict == "no_dates":
        verdict_label = "未设区间"
    else:
        verdict_label = "按计划"

    window_note = {
        "not_started": "未开始",
        "in_window": "进行中",
        "due": "今日应收口",
        "overdue": "已过窗",
        "no_dates": "未设区间",
    }.get(progress["state"], "")
    if verdict == "unknown":
        window_note = "未维护"
    elif verdict == "no_dates":
        window_note = "未设区间"
    elif verdict == "behind" and progress["state"] == "in_window":
        window_note = "进行中·偏慢"
    elif verdict == "behind" and progress["state"] in {"due", "overdue"}:
        window_note = "已过窗·未达标"
    elif verdict == "ahead" and progress["state"] == "in_window":
        window_note = "进行中·偏快"
    elif verdict == "on_track" and progress["state"] in {"due", "overdue"}:
        window_note = "已完成" if actual + 0.6 >= target else window_note
    elif verdict == "on_track" and progress["state"] == "in_window" and actual + 0.6 >= target:
        window_note = "已完成"

    planned_label = _label_number(planned, pct=pct, unit=unit)
    actual_label = _label_number(actual, pct=pct, unit=unit)
    target_label = _label_number(target, pct=pct, unit=unit)
    if not pct and extra.get("total"):
        planned_label = f"{int(round(planned))} / {int(extra['total'])}"
        actual_label = f"{int(round(actual))} / {int(extra['total'])}"

    out.update(
        {
            "windowState": progress["state"],
            "plannedRatio": round(float(progress["ratio"]), 4),
            "plannedValue": _round_value(planned, pct=pct),
            "actualValue": _round_value(actual, pct=pct),
            "targetValue": _round_value(target, pct=pct),
            "plannedLabel": planned_label,
            "actualLabel": actual_label,
            "targetLabel": target_label,
            "verdict": verdict,
            "verdictLabel": verdict_label,
            "tone": tone,
            "unconfigured": unconfigured,
            "windowNote": window_note,
            "daysBehind": round(days_behind, 1),
            "gateTypeLabel": GATE_TYPE_LABEL.get(out["gateType"], out["gateType"]),
            "extra": extra,
        }
    )
    return out


def _sprint_workday_meta(sprint: str, today: date) -> dict[str, Any]:
    start, end = parse_sprint_window(sprint)
    if not start or not end:
        return {
            "start": "",
            "end": "",
            "index": 0,
            "total": 0,
            "remaining": 0,
            "label": "",
        }
    days = workdays(start, end)
    total = len(days)
    index = 0
    for i, d in enumerate(days, start=1):
        if d <= today:
            index = i
    remaining = max(0, total - index)
    label = ""
    if total:
        label = f"第 {index} / {total} 工作日 · 剩余 {remaining} 天"
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "index": index,
        "total": total,
        "remaining": remaining,
        "label": label,
    }


def _auto_risk(phases: list[dict[str, Any]], *, p0p1_open: int) -> str:
    enabled = [p for p in phases if p.get("enabled")]
    if any(p.get("tone") == "danger" for p in enabled):
        return "danger"
    if p0p1_open > 0:
        return "warn"
    if any(p.get("verdict") == "behind" for p in enabled):
        return "warn"
    if any(p.get("verdict") == "unknown" and p.get("gateType") != "review" for p in enabled):
        return "low"
    return "ok"


def _overall_pace(
    phases: list[dict[str, Any]],
    *,
    p0p1_open: int,
) -> tuple[str, str, str]:
    """Header「相对计划」: schedule first; unclosed P0/P1 is not behind-plan."""
    enabled = [p for p in phases if p.get("enabled")]
    danger = next((p for p in enabled if p.get("tone") == "danger"), None)
    behind = next((p for p in enabled if p.get("verdict") == "behind"), None)
    worst = danger or behind
    hint = str((worst or {}).get("verdictLabel") or "")
    if danger:
        return "danger", "有收口风险", hint
    if behind:
        return "warn", "慢了", hint
    if p0p1_open > 0:
        return "warn", "有未关 P0/P1", f"{int(p0p1_open)} 条"
    return "ok", "按计划", ""


def _auto_conclusion(phases: list[dict[str, Any]], risk: str) -> str:
    behind = [p for p in phases if p.get("enabled") and p.get("verdict") == "behind"]
    if not behind:
        if risk in {"ok", "low"}:
            return "各环节按计划线推进，收口窗口暂未见明显压缩。"
        return "计划线已对照，请关注未关缺陷与当日风险申报。"
    parts = []
    for p in behind[:3]:
        parts.append(f"{p['name']}{p.get('verdictLabel') or '落后'}")
    extra = behind[0].get("verdictLabel") or ""
    lead = "、".join(parts)
    if extra and "收口" in (behind[-1].get("name") or ""):
        return f"相对计划偏慢：{lead}。若明日不能追回，结项窗口可能被压缩。"
    return f"相对计划偏慢：{lead}。请按计划线跟进负责人和当日应对。"


def _story_meets_phase_gate(
    row: dict[str, Any],
    phase: dict[str, Any],
    reviews: dict[str, dict[str, str]],
) -> bool | None:
    """本条 Story 是否达到该环节门槛。None = 此环节不适用于该行（跳过）。"""
    gate = str(phase.get("gateType") or "").strip()
    if gate == "exec_pct":
        design = int(row.get("caseNum") or 0)
        if design <= 0:
            return None
        ran = (
            int(row.get("passed") or 0)
            + int(row.get("failed") or 0)
            + int(row.get("blocked") or 0)
        )
        target = float(phase.get("targetPct") or 100) / 100.0
        return (ran / design) + 1e-9 >= target
    if gate == "ready_count":
        return row_is_ready(row)
    if gate == "case_ready":
        return int(row.get("caseNum") or 0) > 0
    if gate == "review":
        name = str(row.get("story") or "")
        rv = reviews.get(name) or {}
        result = rv.get("reviewResult") or row.get("reviewResult") or ""
        return _is_review_pass(result)
    if gate == "accept":
        return str(row.get("storyStatus") or "").strip() in ACCEPT_DONE_STATUSES
    gate_status = str(phase.get("gateStatus") or "").strip() or "开发中"
    need = _status_rank(gate_status)
    rank = _status_rank(str(row.get("storyStatus") or ""))
    if rank < 0:
        return False
    return need < 0 or rank >= need


def _enabled_dated_phases(phases: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for phase in phases or []:
        if not phase.get("enabled"):
            continue
        if parse_iso_date(phase.get("start")) and parse_iso_date(phase.get("end")):
            out.append(phase)
    return out


def plan_ready_deadline(phases: list[dict[str, Any]] | None) -> date | None:
    """已启用「开发提测」环节的窗口结束日；没有则 None。"""
    for phase in _enabled_dated_phases(phases):
        if str(phase.get("id") or "") == "ready":
            return parse_iso_date(phase.get("end"))
    return None


def _story_vs_configured_plan(
    row: dict[str, Any],
    *,
    today: date,
    reviews: dict[str, dict[str, str]],
    phases: list[dict[str, Any]],
) -> dict[str, str]:
    comment = str(row.get("readyComment") or "")
    review = reviews.get(str(row.get("story") or ""), {})
    notes: list[str] = []
    if review.get("reviewResult"):
        notes.append(f"评审{review['reviewResult']}")

    missed: list[dict[str, Any]] = []
    ready_phase: dict[str, Any] | None = None
    for phase in phases:
        if str(phase.get("id") or "") == "ready":
            ready_phase = phase
        start = parse_iso_date(phase.get("start"))
        end = parse_iso_date(phase.get("end"))
        if not start or not end:
            continue
        progress = _window_progress(start, end, today)
        meets = _story_meets_phase_gate(row, phase, reviews)
        if meets is None:
            continue
        if progress["state"] in {"due", "overdue"} and not meets:
            missed.append({"phase": phase, "progress": progress, "end": end})

    if missed:
        order = {pid: i for i, pid in enumerate(_PHASE_BEHIND_PRIORITY)}
        missed.sort(
            key=lambda item: (
                order.get(str(item["phase"].get("id") or ""), 99),
                str(item["phase"].get("end") or ""),
            )
        )
        hit = missed[0]
        phase = hit["phase"]
        end: date = hit["end"]
        tone = "danger" if hit["progress"]["state"] == "overdue" else "warn"
        name = str(phase.get("name") or "计划")
        ready_date = parse_iso_date(row.get("readyDate"))
        if str(phase.get("id") or "") == "ready" and ready_date and ready_date > end:
            delta = (ready_date - end).days
            return {
                "verdict": "behind",
                "tone": tone,
                "label": "Ready Delay",
                "note": comment or f"提测晚 {delta} 天（计划 {fmt_mmdd(end)}）",
            }
        return {
            "verdict": "behind",
            "tone": tone,
            "label": "Behind",
            "note": comment or f"{name}计划 {fmt_mmdd(end)}，尚未达标",
        }

    if ready_phase:
        end = parse_iso_date(ready_phase.get("end"))
        ready_date = parse_iso_date(row.get("readyDate"))
        if end and ready_date and ready_date < end and row_is_ready(row):
            return {
                "verdict": "ahead",
                "tone": "ok",
                "label": "Ahead",
                "note": comment or f"提前 {(end - ready_date).days} 天 Ready",
            }

    blocked = int(row.get("blocked") or 0)
    if blocked:
        notes.append(f"Block×{blocked}")
    return {
        "verdict": "on_track",
        "tone": "ok",
        "label": "On track",
        "note": comment or ("；".join(notes)),
    }


def _story_vs_ready_deadline(
    row: dict[str, Any],
    *,
    today: date,
    reviews: dict[str, dict[str, str]],
    sprint: str = "",
) -> dict[str, str]:
    expected = (
        parse_iso_date(row.get("readyDeadline"))
        or parse_iso_date(row.get("expectedReadyDate"))
        or default_ready_deadline(sprint)
    )
    ready_date = parse_iso_date(row.get("readyDate"))
    ready = row_is_ready(row)
    comment = str(row.get("readyComment") or "")
    review = reviews.get(str(row.get("story") or ""), {})
    notes: list[str] = []
    if review.get("reviewResult"):
        notes.append(f"评审{review['reviewResult']}")
    if "delay" in comment.casefold() or "delay" in comment.lower():
        return {
            "verdict": "behind",
            "tone": "warn",
            "label": "Ready Delay",
            "note": comment,
        }
    if expected and ready_date and ready_date > expected:
        delta = (ready_date - expected).days
        return {
            "verdict": "behind",
            "tone": "warn",
            "label": "Ready Delay",
            "note": comment or f"提测晚 {delta} 天",
        }
    if expected and not ready and today >= expected:
        return {
            "verdict": "behind",
            "tone": "warn",
            "label": "Behind",
            "note": comment or f"计划 {fmt_mmdd(expected)} Ready，尚未提测",
        }
    if expected and ready_date and ready_date < expected:
        return {
            "verdict": "ahead",
            "tone": "ok",
            "label": "Ahead",
            "note": comment or f"提前 {(expected - ready_date).days} 天 Ready",
        }
    blocked = int(row.get("blocked") or 0)
    if blocked:
        notes.append(f"Block×{blocked}")
    return {
        "verdict": "on_track",
        "tone": "ok",
        "label": "On track",
        "note": comment or ("；".join(notes)),
    }


def story_vs_plan(
    row: dict[str, Any],
    *,
    today: date,
    reviews: dict[str, dict[str, str]],
    phases: list[dict[str, Any]] | None = None,
    sprint: str = "",
) -> dict[str, str]:
    """已设计划线（启用且有区间）时按各环节窗口结束门槛判定；否则退回默认提测截止日。"""
    configured = _enabled_dated_phases(phases)
    if configured:
        return _story_vs_configured_plan(
            row, today=today, reviews=reviews, phases=configured
        )
    return _story_vs_ready_deadline(
        row, today=today, reviews=reviews, sprint=sprint
    )


def evaluate_plan(
    plan: dict[str, Any] | None,
    *,
    sprint: str,
    rows: list[dict[str, Any]],
    p0p1_open: int = 0,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or now_beijing().date()
    normalized = normalize_plan(plan)
    reviews = normalized.get("reviews") or {}
    phases = [
        evaluate_phase(
            phase,
            rows,
            today=today,
            reviews=reviews,
            p0p1_open=p0p1_open,
        )
        for phase in normalized["phases"]
    ]
    configured = any(
        p.get("enabled") and parse_iso_date(p.get("start")) and parse_iso_date(p.get("end"))
        for p in phases
    )
    by_id = {p["id"]: p for p in phases}
    compare_rows = []
    titles = {
        "ready": "开发提测（Ready for Test）",
        "exec": "测试执行（相对排期）",
        "accept": "验收收口",
    }
    for pid in COMPARE_PHASE_IDS:
        phase = by_id.get(pid)
        if not phase:
            continue
        compare_rows.append(
            {
                "id": pid,
                "name": titles.get(pid, phase["name"]),
                "plannedLabel": (
                    f"本日应到 {phase['plannedLabel']}"
                    if phase["gateType"] != "exec_pct"
                    else f"本日应完成约 {phase['plannedLabel']}"
                ),
                "actualLabel": phase["actualLabel"],
                "verdict": phase["verdict"],
                "verdictLabel": phase["verdictLabel"],
                "tone": phase["tone"],
                "enabled": phase["enabled"],
            }
        )
    risk = _auto_risk(phases, p0p1_open=p0p1_open)
    workday = _sprint_workday_meta(sprint, today)
    overall_tone, overall_label, overall_hint = _overall_pace(phases, p0p1_open=p0p1_open)
    return {
        "configured": configured,
        "today": today.isoformat(),
        "phases": phases,
        "compareRows": compare_rows,
        "reviews": reviews,
        "risk": risk,
        "riskLabel": RISK_LABEL.get(risk, risk),
        "overallTone": overall_tone,
        "overallLabel": overall_label,
        "overallHint": overall_hint,
        "autoConclusion": _auto_conclusion(phases, risk),
        "workday": workday,
        "suggest": suggest_phase_dates(sprint, normalized["phases"]),
    }

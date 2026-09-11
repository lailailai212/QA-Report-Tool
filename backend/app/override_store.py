from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .config import settings
from .plan_line import normalize_plan
from .timeutil import now_beijing_iso

OVERRIDE_DIR = settings.db_path.parent / "overrides"

# Canonical Test ENV options (order preserved when saving / displaying)
TEST_ENV_OPTIONS = ("SIT", "UAT1", "UAT2", "PRE1", "PRE2", "PROD")

SECTIONS = ("meta", "stories", "reopen", "completion", "retro", "points", "exec", "plan")

OVERALL_RESULT_OPTIONS = ("Pass", "Pass with Risk", "Fail")

DEFAULT_EXIT_CRITERIA = {
    "execPassMin": 95,
    "p0p1OpenMax": 0,
}

DEFAULT_SIGN_OFF = (
    {"role": "QA Owner", "name": "", "result": "", "date": ""},
    {"role": "Dev Owner", "name": "", "result": "", "date": ""},
    {"role": "PM", "name": "", "result": "", "date": ""},
)

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


_RISK_PREFIX_RE = re.compile(r"^\s*风险\s*[：:]\s*")
_ACTION_SPLIT_RE = re.compile(r"(?:^|\n)\s*应对措施\s*[：:]\s*")


def split_risk_fields(risk_block: str | None, risk_action: str | None = None) -> tuple[str, str]:
    """Split stored 风险 / 应对措施. Always peel 应对措施 out of a combined blob."""
    block = str(risk_block or "").strip()
    explicit = str(risk_action or "").strip()
    parsed_risk = ""
    parsed_action = ""
    if block:
        match = _ACTION_SPLIT_RE.search(block)
        if match:
            parsed_risk = _RISK_PREFIX_RE.sub("", block[: match.start()]).strip()
            parsed_action = block[match.end() :].strip()
        else:
            parsed_risk = _RISK_PREFIX_RE.sub("", block).strip()
    return parsed_risk, explicit or parsed_action


def compose_risk_block(risk: str | None, action: str | None = None) -> str:
    """Join 风险 / 应对措施 for consumers that still want one blob."""
    risk_text = str(risk or "").strip()
    action_text = str(action or "").strip()
    parts: list[str] = []
    if risk_text:
        parts.append(f"风险：{risk_text}")
    if action_text:
        parts.append(f"应对措施：{action_text}")
    return "\n".join(parts)


def normalize_risk_rows(
    raw: Any,
    *,
    fallback_risk: str = "",
    fallback_action: str = "",
) -> list[dict[str, str]]:
    """Structured 风险 / 应对措施 / 执行人 rows. Falls back to legacy text fields."""
    rows: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            risk = str(item.get("risk") or "").strip()
            action = str(item.get("action") or "").strip()
            owner = str(item.get("owner") or "").strip()
            if risk or action or owner:
                rows.append({"risk": risk, "action": action, "owner": owner})
    if rows:
        return rows
    risk = str(fallback_risk or "").strip()
    action = str(fallback_action or "").strip()
    if risk or action:
        return [{"risk": risk, "action": action, "owner": ""}]
    return []


def compose_risk_texts_from_rows(rows: list[dict[str, Any]] | None) -> tuple[str, str]:
    """Flatten table rows into legacy 风险 / 应对措施 blobs."""
    cleaned = normalize_risk_rows(rows)
    risks: list[str] = []
    actions: list[str] = []
    numbered = len(cleaned) > 1
    for i, row in enumerate(cleaned, 1):
        prefix = f"{i}. " if numbered else ""
        owner = str(row.get("owner") or "").strip()
        owner_s = f"（{owner}）" if owner else ""
        risk = str(row.get("risk") or "").strip()
        action = str(row.get("action") or "").strip()
        if risk:
            risks.append(f"{prefix}{risk}{owner_s}")
        if action:
            actions.append(f"{prefix}{action}{owner_s}")
    return "\n".join(risks), "\n".join(actions)


def _safe_name(sprint: str) -> str:
    name = (sprint or "").strip()
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name or "_unknown"


def override_path(sprint: str) -> Path:
    return OVERRIDE_DIR / f"{_safe_name(sprint)}.json"


def empty_rev() -> dict[str, int]:
    return {
        "meta": 0,
        "stories": 0,
        "reopen": 0,
        "completion": 0,
        "retro": 0,
        "points": 0,
        "exec": 0,
        "plan": 0,
    }


def empty_retro() -> dict[str, Any]:
    return {
        "highlights": "",
        "concerns": "",
        "nextFocus": "",
        "scopeChangeNotes": [],
        "reopenRows": [],
        "workdays": None,
        "membersNote": "",
        "sprintWindowStart": "",
        "sprintWindowEnd": "",
    }


def normalize_retro(raw: Any) -> dict[str, Any]:
    base = empty_retro()
    if not isinstance(raw, dict):
        return base

    base["highlights"] = str(raw.get("highlights") or "")
    base["concerns"] = str(raw.get("concerns") or "")
    base["nextFocus"] = str(raw.get("nextFocus") or "")
    base["membersNote"] = str(raw.get("membersNote") or "").strip()
    base["sprintWindowStart"] = str(raw.get("sprintWindowStart") or "").strip()
    base["sprintWindowEnd"] = str(raw.get("sprintWindowEnd") or "").strip()

    workdays = raw.get("workdays")
    if workdays is None or workdays == "":
        base["workdays"] = None
    else:
        try:
            base["workdays"] = max(0, int(workdays))
        except (TypeError, ValueError):
            base["workdays"] = None

    notes_raw = raw.get("scopeChangeNotes")
    if isinstance(notes_raw, list):
        notes: list[dict[str, str]] = []
        for row in notes_raw:
            if not isinstance(row, dict):
                continue
            summary = str(row.get("summary") or "").strip()
            if not summary:
                continue
            notes.append(
                {
                    "summary": summary,
                    "type": str(row.get("type") or "").strip(),
                    "op": str(row.get("op") or "需求变更").strip() or "需求变更",
                    "remark": str(row.get("remark") or "").strip(),
                }
            )
        base["scopeChangeNotes"] = notes

    reopen_raw = raw.get("reopenRows")
    if isinstance(reopen_raw, list):
        rows: list[dict[str, Any]] = []
        for r in reopen_raw:
            if not isinstance(r, dict):
                continue
            summary = str(r.get("summary") or r.get("name") or "").strip()
            if not summary:
                continue
            try:
                times = int(r.get("reopenTimes") or 0)
            except (TypeError, ValueError):
                times = 0
            rows.append(
                {
                    "priority": str(r.get("priority") or "").strip(),
                    "summary": summary,
                    "url": str(r.get("url") or "").strip(),
                    "reopenTimes": times,
                }
            )
        base["reopenRows"] = rows

    return base


def empty_points() -> dict[str, Any]:
    return {"people": []}


def _as_opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_points(raw: Any) -> dict[str, Any]:
    base = empty_points()
    if not isinstance(raw, dict):
        return base
    people_raw = raw.get("people")
    if not isinstance(people_raw, list):
        return base
    people: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in people_raw:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        people.append(
            {
                "name": name,
                "initialPoints": _as_opt_float(row.get("initialPoints")),
                "otherNote": str(row.get("otherNote") or ""),
                "otherNoteManual": bool(row.get("otherNoteManual")),
                "otherPoints": _as_opt_float(row.get("otherPoints")),
                "otherPointsManual": bool(row.get("otherPointsManual")),
                "regression": _as_opt_float(row.get("regression")) or 0.0,
                "rollback": _as_opt_float(row.get("rollback")) or 0.0,
                "hidden": bool(row.get("hidden")),
            }
        )
    base["people"] = people
    return base


def empty_completion() -> dict[str, Any]:
    return {
        "overallResult": "",
        "testOwner": "",
        "testWindowStart": "",
        "testWindowEnd": "",
        "exitCriteria": dict(DEFAULT_EXIT_CRITERIA),
        "summaryOneLiner": "",
        "deferredItems": "",
        "recommendations": "",
        "signOff": [dict(row) for row in DEFAULT_SIGN_OFF],
        "openBugNotes": {},
    }


def normalize_completion(raw: Any) -> dict[str, Any]:
    base = empty_completion()
    if not isinstance(raw, dict):
        return base

    overall = str(raw.get("overallResult") or "").strip()
    if overall in OVERALL_RESULT_OPTIONS:
        base["overallResult"] = overall
    base["testOwner"] = str(raw.get("testOwner") or "").strip()
    base["testWindowStart"] = str(raw.get("testWindowStart") or "").strip()
    base["testWindowEnd"] = str(raw.get("testWindowEnd") or "").strip()
    base["summaryOneLiner"] = str(raw.get("summaryOneLiner") or "").strip()
    base["deferredItems"] = str(raw.get("deferredItems") or "")
    base["recommendations"] = str(raw.get("recommendations") or "")

    criteria_raw = raw.get("exitCriteria")
    if isinstance(criteria_raw, dict):
        try:
            exec_min = int(criteria_raw.get("execPassMin"))
        except (TypeError, ValueError):
            exec_min = DEFAULT_EXIT_CRITERIA["execPassMin"]
        try:
            p0p1_max = int(criteria_raw.get("p0p1OpenMax"))
        except (TypeError, ValueError):
            p0p1_max = DEFAULT_EXIT_CRITERIA["p0p1OpenMax"]
        base["exitCriteria"] = {
            "execPassMin": max(0, min(100, exec_min)),
            "p0p1OpenMax": max(0, p0p1_max),
        }

    sign_raw = raw.get("signOff")
    if isinstance(sign_raw, list) and sign_raw:
        cleaned_sign: list[dict[str, str]] = []
        for row in sign_raw:
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or "").strip()
            if not role:
                continue
            cleaned_sign.append(
                {
                    "role": role,
                    "name": str(row.get("name") or "").strip(),
                    "result": str(row.get("result") or "").strip(),
                    "date": str(row.get("date") or "").strip(),
                }
            )
        if cleaned_sign:
            base["signOff"] = cleaned_sign

    notes_raw = raw.get("openBugNotes")
    if isinstance(notes_raw, dict):
        notes: dict[str, str] = {}
        for key, value in notes_raw.items():
            k = str(key or "").strip()
            if not k:
                continue
            notes[k] = str(value or "").strip()
        base["openBugNotes"] = notes

    return base


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


def empty_exec() -> dict[str, Any]:
    return {
        "overallStatus": "",
        "oneLiner": "",
        "highlight": "",
        "concern": "",
        "sectionLeads": {},
        "summaryKv": {},
        "goals": [],
        "risks": [],
        "mgmtRequests": [],
        "metricInsights": [],
        "capacityFactors": [],
        "nextSprint": {},
        "scopeMovedIn": [],
        "scopeMovedOut": [],
        "scopeSummary": {},
        "scopePlanNote": "",
        "incompleteNotes": {},
        "compareSprint": "",
        "reportOwner": "",
        "productionMetrics": {},
        "workdays": None,
        "membersNote": "",
        "teamComposition": "",
        "qualityScoreDetail": "",
        "deliveryScopeNotes": {},
    }


def normalize_exec(raw: Any) -> dict[str, Any]:
    base = empty_exec()
    if not isinstance(raw, dict):
        return base
    if raw.get("overallStatus") in ("ok", "low", "warn", "danger"):
        base["overallStatus"] = raw["overallStatus"]
    for key in (
        "oneLiner",
        "highlight",
        "concern",
        "compareSprint",
        "reportOwner",
        "membersNote",
        "teamComposition",
        "qualityScoreDetail",
    ):
        if key in raw:
            base[key] = str(raw.get(key) or "")
    workdays = raw.get("workdays")
    if workdays is None or workdays == "":
        base["workdays"] = None
    else:
        try:
            base["workdays"] = max(0, int(workdays))
        except (TypeError, ValueError):
            base["workdays"] = None
    if isinstance(raw.get("sectionLeads"), dict):
        base["sectionLeads"] = {str(k): str(v or "") for k, v in raw["sectionLeads"].items()}
    if isinstance(raw.get("summaryKv"), dict):
        base["summaryKv"] = {str(k): str(v or "") for k, v in raw["summaryKv"].items()}
    for key in (
        "goals",
        "risks",
        "mgmtRequests",
        "metricInsights",
        "capacityFactors",
        "scopeMovedIn",
        "scopeMovedOut",
    ):
        if isinstance(raw.get(key), list):
            base[key] = raw[key]
    if isinstance(raw.get("scopeSummary"), dict):
        base["scopeSummary"] = raw["scopeSummary"]
    if "scopePlanNote" in raw:
        base["scopePlanNote"] = str(raw.get("scopePlanNote") or "")
    if isinstance(raw.get("deliveryScopeNotes"), dict):
        base["deliveryScopeNotes"] = {str(k): str(v or "") for k, v in raw["deliveryScopeNotes"].items()}
    if isinstance(raw.get("nextSprint"), dict):
        base["nextSprint"] = {str(k): str(v or "") for k, v in raw["nextSprint"].items()}
    if isinstance(raw.get("productionMetrics"), dict):
        base["productionMetrics"] = {str(k): str(v or "") for k, v in raw["productionMetrics"].items()}
    if isinstance(raw.get("incompleteNotes"), dict):
        cleaned: dict[str, dict[str, str]] = {}
        for k, v in raw["incompleteNotes"].items():
            if not isinstance(v, dict):
                continue
            cleaned[str(k)] = {
                "delayReason": str(v.get("delayReason") or ""),
                "riskReportReason": str(v.get("riskReportReason") or ""),
            }
        base["incompleteNotes"] = cleaned
    return base


def empty_override(sprint: str = "") -> dict[str, Any]:
    return {
        "sprint": sprint or "",
        "updatedAt": None,
        "rev": empty_rev(),
        "testEnv": "",
        "riskBlock": "",
        "riskAction": "",
        "riskRows": [],
        "dailyConclusion": "",
        "attention": "",
        "riskLevel": "",
        "stories": {},
        "reopenRows": None,
        "completion": empty_completion(),
        "retro": empty_retro(),
        "points": empty_points(),
        "exec": empty_exec(),
        "plan": normalize_plan(None),
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
    risk, action = split_risk_fields(data.get("riskBlock") or "", data.get("riskAction"))
    base["riskBlock"] = risk
    base["riskAction"] = action
    base["riskRows"] = normalize_risk_rows(
        data.get("riskRows"),
        fallback_risk=risk,
        fallback_action=action,
    )
    base["dailyConclusion"] = str(data.get("dailyConclusion") or "")
    base["attention"] = str(data.get("attention") or "")
    risk_level = str(data.get("riskLevel") or "").strip()
    base["riskLevel"] = risk_level if risk_level in ("ok", "low", "warn", "danger") else ""
    base["plan"] = normalize_plan(data.get("plan"))
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
    base["completion"] = normalize_completion(data.get("completion"))
    base["retro"] = normalize_retro(data.get("retro"))
    base["points"] = normalize_points(data.get("points"))
    base["exec"] = normalize_exec(data.get("exec"))
    return base


def _touched_sections(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if any(
        k in payload
        for k in ("testEnv", "riskBlock", "riskAction", "riskRows", "dailyConclusion", "attention", "riskLevel")
    ):
        out.append("meta")
    if "stories" in payload:
        out.append("stories")
    if "reopenRows" in payload:
        out.append("reopen")
    if "completion" in payload:
        out.append("completion")
    if "retro" in payload:
        out.append("retro")
    if "points" in payload:
        out.append("points")
    if "exec" in payload:
        out.append("exec")
    if "plan" in payload:
        out.append("plan")
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
        if "riskRows" in payload:
            rows = normalize_risk_rows(payload.get("riskRows"))
            current["riskRows"] = rows
            risk, action = compose_risk_texts_from_rows(rows)
            current["riskBlock"] = risk
            current["riskAction"] = action
        elif "riskBlock" in payload or "riskAction" in payload:
            block = (
                payload.get("riskBlock")
                if "riskBlock" in payload
                else current.get("riskBlock")
            )
            action_in = (
                payload.get("riskAction")
                if "riskAction" in payload
                else current.get("riskAction")
            )
            risk, action = split_risk_fields(str(block or ""), str(action_in or ""))
            current["riskBlock"] = risk
            current["riskAction"] = action
            current["riskRows"] = normalize_risk_rows(
                None,
                fallback_risk=risk,
                fallback_action=action,
            )
        if "dailyConclusion" in payload:
            current["dailyConclusion"] = str(payload.get("dailyConclusion") or "")
        if "attention" in payload:
            current["attention"] = str(payload.get("attention") or "")
        if "riskLevel" in payload:
            level = str(payload.get("riskLevel") or "").strip()
            current["riskLevel"] = (
                level if level in ("ok", "low", "warn", "danger") else ""
            )

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

        if "completion" in payload:
            current["completion"] = normalize_completion(payload.get("completion"))

        if "retro" in payload:
            current["retro"] = normalize_retro(payload.get("retro"))

        if "points" in payload:
            current["points"] = normalize_points(payload.get("points"))

        if "exec" in payload:
            incoming = payload.get("exec")
            if isinstance(incoming, dict):
                current["exec"] = normalize_exec({**(current.get("exec") or {}), **incoming})
            else:
                current["exec"] = normalize_exec(incoming)

        if "plan" in payload:
            current["plan"] = normalize_plan(payload.get("plan"))

        for section in touching:
            rev[section] = int(rev[section]) + 1
        current["rev"] = rev
        current["sprint"] = sprint
        current["updatedAt"] = now
        _atomic_write_json(override_path(sprint), current)
        return current

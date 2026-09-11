"""Refresh Sprint retro snapshots (Story/Task/TI/Bug + Story Points)."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import ROOT, settings
from .feishu_mcp import FeishuMcpError, fetch_retro_raw, format_mcp_exception
from .feishu_refresh import (
    FeishuRefreshBusy,
    FeishuRefreshConfigError,
    _as_str,
    _field_map,
    _label,
    _norm_bug_status,
    _pick_field,
)
from .timeutil import now_beijing, now_beijing_iso

logger = logging.getLogger(__name__)

RETRO_DIR = ROOT / "exports" / "retro"
LOCK_PATH = RETRO_DIR / ".refresh.lock"
STATUS_PATH = RETRO_DIR / ".last_refresh.json"
_THREAD_LOCK = threading.Lock()

SPACE_NAME = "OBIS"

STORY_MQL = (
    "SELECT `Item Id`, `Summary`, `Status`, `Story Point (DEV)`, `Story Point (QC)`, "
    f"`__产品经理`, `__Dev Owner`, `__QC Owner` FROM `{SPACE_NAME}`.`User Story` "
    "WHERE array_contains(`Sprint`, '{sprint}')"
)
TASK_MQL = (
    "SELECT `Item Id`, `Summary`, `Status`, `Story Points`, `__Task Owner` "
    f"FROM `{SPACE_NAME}`.`Task` "
    "WHERE array_contains(`Sprint`, '{sprint}')"
)
TI_MQL = (
    "SELECT `Item Id`, `Summary`, `Status`, `Story Points (DEV)`, `Story Points (QC)`, "
    f"`__Dev Owner`, `__QC Owner` FROM `{SPACE_NAME}`.`Tech Improvement` "
    "WHERE array_contains(`Sprint`, '{sprint}')"
)
BUG_MQL = (
    "SELECT `Item Id`, `Summary`, `Status`, `Priority`, `Severity`, "
    f"`Root Cause Category`, `Bug Environment`, `Story Points`, `__Dev Owner` "
    f"FROM `{SPACE_NAME}`.`Bug` "
    "WHERE array_contains(`Sprint`, '{sprint}')"
)

RETRO_RULES = {
    "pointSource": "story_point_fields",
    "itemDoneStatuses": ["已验收", "待验收"],
    "pointDoneStatuses": ["已验收", "待验收"],
    "bugFixedStatuses": ["Done", "Closed", "Converted"],
    "severityWeights": {
        "Critical": 10,
        "Major": 5,
        "Medium": 2,
        "Minor": 1,
        "Trivial": 0.5,
    },
    "rootCauseEmpty": "TBD",
    # 单条工作项 Story Point 上限。飞书偶发把乱填值写进 Story Points（如 12312），不得进入汇总。
    "maxItemStoryPoints": 40,
}

# 飞书 Owner / QC Owner 会把人分错表；人名以展示名为准。
# LEAD：不进 DEV / QC 人力，也不计入 DEV/QC 估分。
PERSON_ROLE_OVERRIDE = {
    "张峰": "QC",
    "刘梦豪": "DEV",
    "施金华": "LEAD",
}


def is_lead_person(name: str) -> bool:
    return PERSON_ROLE_OVERRIDE.get((name or "").strip()) == "LEAD"


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if value.get("double_value") is not None:
            try:
                return float(value.get("double_value") or 0)
            except (TypeError, ValueError):
                return 0.0
        if value.get("long_value") is not None:
            try:
                return float(value.get("long_value") or 0)
            except (TypeError, ValueError):
                return 0.0
        if value.get("number_value") is not None:
            try:
                return float(value.get("number_value") or 0)
            except (TypeError, ValueError):
                return 0.0
        # unwrap nested MCP shapes
        for key in ("value", "field_value"):
            if key in value:
                return _as_float(value.get(key))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0
    if isinstance(value, list) and value:
        return _as_float(value[0])
    return 0.0


MAX_ITEM_STORY_POINTS = float(RETRO_RULES["maxItemStoryPoints"])


def _sane_point(raw: Any) -> float:
    n = _as_float(raw)
    if n < 0 or n > MAX_ITEM_STORY_POINTS:
        return 0.0
    return n


def item_story_points(row: dict[str, Any]) -> tuple[float, float]:
    """DEV/QC points for one item; implausible Feishu values become 0."""
    return _sane_point(row.get("pointDev")), _sane_point(row.get("pointQc"))


def is_point_outlier(row: dict[str, Any]) -> bool:
    for key in ("pointDev", "pointQc"):
        n = _as_float(row.get(key))
        if n < 0 or n > MAX_ITEM_STORY_POINTS:
            return True
    return False


def list_point_outliers(
    *groups: tuple[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for label, rows in groups:
        for r in rows or []:
            if not isinstance(r, dict) or not is_point_outlier(r):
                continue
            out.append(
                {
                    "type": label,
                    "id": r.get("id") or "",
                    "summary": r.get("summary") or "",
                    "owner": r.get("owner") or "",
                    "status": r.get("status") or "",
                    "pointDev": _as_float(r.get("pointDev")),
                    "pointQc": _as_float(r.get("pointQc")),
                    "url": r.get("url") or "",
                }
            )
    return out


def _as_users(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        users = value.get("user_value_list")
        if isinstance(users, list):
            names: list[str] = []
            for u in users:
                if not isinstance(u, dict):
                    continue
                name = (
                    str(u.get("name_cn") or "").strip()
                    or str(u.get("name_en") or "").strip()
                    or str(u.get("user_key") or "").strip()
                )
                if name:
                    names.append(name)
            return names
        nested = value.get("value")
        if nested is not None:
            return _as_users(nested)
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            out.extend(_as_users(x) if isinstance(x, (dict, list)) else [])
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    s = _as_str(value)
    return [s] if s else []


def _as_root_cause(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        cascade = value.get("cascade_key_label_value")
        if isinstance(cascade, dict) and cascade.get("label") is not None:
            return str(cascade["label"]).strip()
        label = _label(value)
        if label:
            return label
    return _label(value)


def _item_url(kind: str, item_id: str) -> str:
    simple = settings.feishu_simple_name
    path = {
        "story": "userstory",
        "task": "othertask",
        "ti": "technical",
        "bug": "bug",
    }.get(kind, "userstory")
    return f"https://project.feishu.cn/{simple}/{path}/detail/{item_id}"


def map_story_row(item: dict[str, Any]) -> dict[str, Any]:
    fields = _field_map(item)
    item_id = _as_str(_pick_field(fields, "Item Id", "item_id"))
    summary = _as_str(_pick_field(fields, "Summary", "summary"))
    status = _label(_pick_field(fields, "Status", "status"))
    pm_owners = _as_users(_pick_field(fields, "__产品经理", "产品经理"))
    dev_owners = _as_users(_pick_field(fields, "__Dev Owner", "Dev Owner"))
    qc_owners = _as_users(_pick_field(fields, "__QC Owner", "QC Owner"))
    return {
        "id": item_id,
        "summary": summary,
        "status": status,
        "pointDev": _as_float(_pick_field(fields, "Story Point (DEV)")),
        "pointQc": _as_float(_pick_field(fields, "Story Point (QC)")),
        "pmOwners": pm_owners,
        "devOwners": dev_owners,
        "qcOwners": qc_owners,
        "owner": dev_owners[0] if dev_owners else "",
        "url": _item_url("story", item_id),
    }


def map_task_row(item: dict[str, Any]) -> dict[str, Any]:
    fields = _field_map(item)
    item_id = _as_str(_pick_field(fields, "Item Id", "item_id"))
    summary = _as_str(_pick_field(fields, "Summary", "summary"))
    status = _label(_pick_field(fields, "Status", "status"))
    owners = _as_users(_pick_field(fields, "__Task Owner", "Task Owner"))
    sp = _as_float(_pick_field(fields, "Story Points"))
    return {
        "id": item_id,
        "summary": summary,
        "status": status,
        "pointDev": sp,
        "pointQc": 0.0,
        "taskOwners": owners,
        "owner": owners[0] if owners else "",
        "url": _item_url("task", item_id),
    }


def map_ti_row(item: dict[str, Any]) -> dict[str, Any]:
    fields = _field_map(item)
    item_id = _as_str(_pick_field(fields, "Item Id", "item_id"))
    summary = _as_str(_pick_field(fields, "Summary", "summary"))
    status = _label(_pick_field(fields, "Status", "status"))
    dev_owners = _as_users(_pick_field(fields, "__Dev Owner", "Dev Owner"))
    qc_owners = _as_users(_pick_field(fields, "__QC Owner", "QC Owner"))
    return {
        "id": item_id,
        "summary": summary,
        "status": status,
        "pointDev": _as_float(_pick_field(fields, "Story Points (DEV)")),
        "pointQc": _as_float(_pick_field(fields, "Story Points (QC)")),
        "devOwners": dev_owners,
        "qcOwners": qc_owners,
        "owner": dev_owners[0] if dev_owners else "",
        "url": _item_url("ti", item_id),
    }


def map_bug_row(item: dict[str, Any]) -> dict[str, Any]:
    fields = _field_map(item)
    item_id = _as_str(_pick_field(fields, "Item Id", "item_id"))
    title = _as_str(_pick_field(fields, "Summary", "summary"))
    status = _norm_bug_status(_label(_pick_field(fields, "Status", "status")))
    priority = _label(_pick_field(fields, "Priority", "priority")).upper() or "P3"
    if priority not in {"P0", "P1", "P2", "P3"}:
        m = re.search(r"P[0-3]", priority)
        priority = m.group(0) if m else "P3"
    severity = _label(_pick_field(fields, "Severity", "severity"))
    root = _as_root_cause(_pick_field(fields, "Root Cause Category"))
    environment = _label(
        _pick_field(fields, "Bug Environment", "bug environment", "field_79f4fc")
    )
    owners = _as_users(_pick_field(fields, "__Dev Owner", "Dev Owner"))
    return {
        "id": item_id,
        "summary": title,
        "status": status,
        "priority": priority,
        "severity": severity,
        "rootCause": root,
        "environment": environment,
        "pointDev": _as_float(_pick_field(fields, "Story Points")),
        "pointQc": 0.0,
        "devOwners": owners,
        "owner": owners[0] if owners else "",
        "url": _item_url("bug", item_id),
    }


def _safe_sprint_name(sprint: str) -> str:
    name = (sprint or "").strip()
    return re.sub(r'[<>:"/\\|?*]', "_", name) or "_unknown"


def snapshot_path(sprint: str) -> Path:
    return RETRO_DIR / f"{_safe_sprint_name(sprint)}_latest.json"


def plan_path(sprint: str) -> Path:
    return RETRO_DIR / f"{_safe_sprint_name(sprint)}_plan.json"


def load_retro_snapshot(sprint: str) -> dict[str, Any] | None:
    path = snapshot_path(sprint)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_retro_plan(sprint: str) -> dict[str, Any] | None:
    path = plan_path(sprint)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_retro_latest_files() -> list[Path]:
    if not RETRO_DIR.exists():
        return []
    return sorted(RETRO_DIR.glob("*_latest.json"))


@contextmanager
def _refresh_lock() -> Iterator[None]:
    if not _THREAD_LOCK.acquire(blocking=False):
        raise FeishuRefreshBusy("复盘快照刷新进行中，请稍后再试")
    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except OSError:
                age = 0
            if age > 1800:
                try:
                    LOCK_PATH.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                except FileExistsError as exc:
                    raise FeishuRefreshBusy(
                        "复盘快照刷新进行中（文件锁），请稍后再试"
                    ) from exc
            else:
                raise FeishuRefreshBusy("复盘快照刷新进行中（文件锁），请稍后再试")
        try:
            os.write(fd, f"{os.getpid()} {now_beijing_iso()}\n".encode())
            yield
        finally:
            os.close(fd)
            try:
                LOCK_PATH.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        _THREAD_LOCK.release()


def read_last_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {"ok": None, "message": "尚无复盘快照刷新记录"}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"ok": None, "message": "状态文件损坏"}
    except (OSError, json.JSONDecodeError):
        return {"ok": None, "message": "状态文件不可读"}


def _write_status(payload: dict[str, Any]) -> None:
    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate_counts(
    payload: dict[str, Any],
    *,
    expect_stories: int,
    expect_tasks: int,
    expect_tis: int,
    expect_bugs: int,
) -> list[str]:
    errors: list[str] = []
    checks = (
        ("stories", expect_stories),
        ("tasks", expect_tasks),
        ("techImprovements", expect_tis),
        ("bugs", expect_bugs),
    )
    for key, expect in checks:
        rows = payload.get(key) or []
        if not isinstance(rows, list):
            errors.append(f"{key} 不是列表")
            continue
        if expect and len(rows) != expect:
            errors.append(f"{key} 条数 {len(rows)} != 期望 {expect}")
        missing_id = sum(1 for r in rows if isinstance(r, dict) and not r.get("id"))
        if missing_id:
            errors.append(f"{key} 有 {missing_id} 条缺少 id")
    return errors


def refresh_retro_sprint(sprint: str, *, trigger: str = "manual") -> dict[str, Any]:
    sprint = (sprint or "").strip()
    if not sprint:
        raise ValueError("sprint 不能为空")
    if not (settings.mcp_user_token or "").strip():
        raise FeishuRefreshConfigError("未配置 MCP_USER_TOKEN，无法刷新复盘快照")

    started = now_beijing_iso()
    try:
        with _refresh_lock():
            logger.info("retro refresh start sprint=%s trigger=%s", sprint, trigger)
            raw = fetch_retro_raw(
                sprint,
                story_mql=STORY_MQL,
                task_mql=TASK_MQL,
                ti_mql=TI_MQL,
                bug_mql=BUG_MQL,
            )
            feishu_sprint = str(raw["feishuSprint"])
            note = raw.get("sprintResolveNote")
            stories = [map_story_row(x) for x in raw["stories"]]
            tasks = [map_task_row(x) for x in raw["tasks"]]
            tis = [map_ti_row(x) for x in raw["techImprovements"]]
            bugs = [map_bug_row(x) for x in raw["bugs"]]
            fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = {
                "sprint": sprint,
                "feishuSprint": feishu_sprint,
                "source": "mcp",
                "fetchedAt": fetched_at,
                "projectKey": settings.feishu_project_key,
                "simpleName": settings.feishu_simple_name,
                "rules": RETRO_RULES,
                "stories": stories,
                "tasks": tasks,
                "techImprovements": tis,
                "bugs": bugs,
            }
            if note:
                payload["sprintResolveNote"] = note
            errors = _validate_counts(
                payload,
                expect_stories=int(raw["expectStories"]),
                expect_tasks=int(raw["expectTasks"]),
                expect_tis=int(raw["expectTechImprovements"]),
                expect_bugs=int(raw["expectBugs"]),
            )
            if errors:
                raise FeishuMcpError("复盘快照校验失败: " + "; ".join(errors))

            stamp = now_beijing().strftime("%Y%m%d_%H%M%S")
            safe = _safe_sprint_name(sprint)
            archive = RETRO_DIR / f"{safe}_{stamp}.json"
            latest = snapshot_path(sprint)
            RETRO_DIR.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            archive.write_text(text, encoding="utf-8")
            tmp = latest.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(latest)

            result = {
                "ok": True,
                "sprint": sprint,
                "feishuSprint": feishu_sprint,
                "sprintResolveNote": note,
                "trigger": trigger,
                "startedAt": started,
                "finishedAt": now_beijing_iso(),
                "fetchedAt": fetched_at,
                "stories": len(stories),
                "tasks": len(tasks),
                "techImprovements": len(tis),
                "bugs": len(bugs),
                "archive": str(archive.relative_to(ROOT)).replace("\\", "/"),
                "latest": str(latest.relative_to(ROOT)).replace("\\", "/"),
            }
            _write_status(result)
            logger.info(
                "retro refresh ok sprint=%s stories=%s tasks=%s ti=%s bugs=%s",
                sprint,
                len(stories),
                len(tasks),
                len(tis),
                len(bugs),
            )
            return result
    except Exception as exc:  # noqa: BLE001
        fail = {
            "ok": False,
            "sprint": sprint,
            "trigger": trigger,
            "startedAt": started,
            "finishedAt": now_beijing_iso(),
            "message": format_mcp_exception(exc),
        }
        try:
            _write_status(fail)
        except OSError:
            pass
        raise


def freeze_retro_plan(sprint: str) -> dict[str, Any]:
    """Freeze current snapshot item ids as plan baseline."""
    snap = load_retro_snapshot(sprint)
    if not snap:
        raise FileNotFoundError(f"尚无复盘快照，请先刷新: {sprint}")

    def _brief(rows: list[Any]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            iid = str(r.get("id") or "").strip()
            if not iid:
                continue
            out.append(
                {
                    "id": iid,
                    "summary": str(r.get("summary") or "").strip(),
                }
            )
        return out

    payload = {
        "sprint": sprint,
        "feishuSprint": snap.get("feishuSprint") or sprint,
        "frozenAt": now_beijing_iso(),
        "sourceFetchedAt": snap.get("fetchedAt"),
        "stories": _brief(list(snap.get("stories") or [])),
        "tasks": _brief(list(snap.get("tasks") or [])),
        "techImprovements": _brief(list(snap.get("techImprovements") or [])),
    }
    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    path = plan_path(sprint)
    tmp = path.with_suffix(".json.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return {
        "ok": True,
        "sprint": sprint,
        "frozenAt": payload["frozenAt"],
        "stories": len(payload["stories"]),
        "tasks": len(payload["tasks"]),
        "techImprovements": len(payload["techImprovements"]),
        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
    }


def plan_status(sprint: str) -> dict[str, Any]:
    plan = load_retro_plan(sprint)
    if not plan:
        return {"exists": False, "sprint": sprint}
    return {
        "exists": True,
        "sprint": sprint,
        "frozenAt": plan.get("frozenAt"),
        "sourceFetchedAt": plan.get("sourceFetchedAt"),
        "stories": len(plan.get("stories") or []),
        "tasks": len(plan.get("tasks") or []),
        "techImprovements": len(plan.get("techImprovements") or []),
    }

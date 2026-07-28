"""Refresh Feishu sprint snapshots via MCP; validate before overwriting _latest."""
from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import ROOT, settings
from .feishu_mcp import FeishuMcpError, fetch_sprint_raw
from .feishu_snapshot import SNAPSHOT_DIR, derive_comment, derive_ready
from .timeutil import now_beijing, now_beijing_iso

logger = logging.getLogger(__name__)

LOCK_PATH = SNAPSHOT_DIR / ".refresh.lock"
STATUS_PATH = SNAPSHOT_DIR / ".last_refresh.json"
_THREAD_LOCK = threading.Lock()

SPACE_NAME = "OBIS"

STORY_MQL = (
    "SELECT `Item Id`, `Summary`, `Status`, status_time('待测试'), "
    "get_node_attribute('开发','__排期_结束时间') "
    f"FROM `{SPACE_NAME}`.`User Story` "
    "WHERE `Sprint` = '{sprint}'"
)
BUG_MQL = (
    "SELECT `Item Id`, `Summary`, `Status`, `Priority` "
    f"FROM `{SPACE_NAME}`.`Bug` "
    "WHERE `Sprint` = '{sprint}'"
)

SNAPSHOT_RULES = {
    "readyYesStatuses": ["待测试", "测试中", "待验收"],
    "readyDateFrom": "status_enter_待测试",
    "expectedReadyDateFrom": "开发节点排期结束日_max",
    "delayComment": "提测Delay",
    "reopen": (
        "Testing/测试中 then To Do count; currently stubbed to 0 "
        "due to MCP op_record 7-day limit"
    ),
}


class FeishuRefreshBusy(RuntimeError):
    pass


class FeishuRefreshConfigError(RuntimeError):
    pass


class FeishuValidateError(RuntimeError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _load_validate_fn():
    path = (
        ROOT
        / ".cursor"
        / "skills"
        / "feishu-sprint-snapshot"
        / "scripts"
        / "validate_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location("feishu_validate_snapshot", path)
    if spec is None or spec.loader is None:
        raise FeishuRefreshConfigError(f"无法加载校验脚本: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.validate


def _field_map(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in item.get("moql_field_list") or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if name:
            out[name] = f.get("value")
        key = str(f.get("key") or "").strip()
        if key and key not in out:
            out[key] = f.get("value")
    return out


def _label_from_dict(value: dict[str, Any]) -> str | None:
    """Extract human label from MCP key/label value shapes."""
    for list_key in ("key_label_value_list", "key_label_value"):
        kl = value.get(list_key)
        if isinstance(kl, dict) and kl.get("label") is not None:
            return str(kl["label"]).strip()
        if isinstance(kl, list) and kl:
            first = kl[0]
            if isinstance(first, dict) and first.get("label") is not None:
                return str(first["label"]).strip()
    if value.get("label") is not None:
        return str(value["label"]).strip()
    return None


def _maybe_parse_dict_str(raw: str) -> Any:
    """Recover when MCP value was accidentally stringified via str(dict)."""
    s = (raw or "").strip()
    if not (s.startswith("{") and "key_label" in s):
        return None
    try:
        parsed = ast.literal_eval(s)
        return parsed if isinstance(parsed, dict) else None
    except (SyntaxError, ValueError):
        return None


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        parsed = _maybe_parse_dict_str(value)
        if parsed is not None:
            return _as_str(parsed)
        return value.strip()
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, dict):
        label = _label_from_dict(value)
        if label:
            return label
        if "string_value" in value and value["string_value"] is not None:
            return str(value["string_value"]).strip()
        if "long_value" in value and value["long_value"] is not None:
            return str(value["long_value"]).strip()
    if isinstance(value, list):
        parts = [_as_str(x) for x in value]
        return next((p for p in parts if p), "")
    return str(value).strip()


def _label(value: Any) -> str:
    if isinstance(value, str):
        parsed = _maybe_parse_dict_str(value)
        if parsed is not None:
            return _label(parsed)
    if isinstance(value, dict):
        label = _label_from_dict(value)
        if label:
            return label
    if isinstance(value, list) and value:
        return _label(value[0]) or _as_str(value[0])
    return _as_str(value)


def _date_yyyy_mm_dd(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else ""


def _max_date_from_value(value: Any) -> str:
    dates: list[str] = []
    if isinstance(value, dict):
        svl = value.get("string_value_list")
        if isinstance(svl, list):
            for x in svl:
                d = _date_yyyy_mm_dd(_as_str(x))
                if d:
                    dates.append(d)
        d = _date_yyyy_mm_dd(_as_str(value))
        if d:
            dates.append(d)
    elif isinstance(value, list):
        for x in value:
            d = _date_yyyy_mm_dd(_as_str(x))
            if d:
                dates.append(d)
    else:
        d = _date_yyyy_mm_dd(_as_str(value))
        if d:
            dates.append(d)
    return max(dates) if dates else ""


def _pick_field(fields: dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in fields and fields[n] is not None:
            return fields[n]
    # substring fallback for localized names
    for n in names:
        for k, v in fields.items():
            if n in k and v is not None:
                return v
    return None


def _norm_bug_status(status: str) -> str:
    s = (status or "").strip()
    mapping = {
        "测试中": "Testing",
        "to do": "To Do",
        "testing": "Testing",
        "fixing": "Fixing",
        "confirming": "Confirming",
        "clarifying": "Clarifying",
        "done": "Done",
        "closed": "Closed",
    }
    return mapping.get(s.lower(), mapping.get(s, s))


def map_story(item: dict[str, Any]) -> dict[str, Any]:
    fields = _field_map(item)
    item_id = _as_str(_pick_field(fields, "Item Id", "item_id"))
    name = _as_str(_pick_field(fields, "Summary", "summary"))
    status = _label(_pick_field(fields, "Status", "status"))
    ready_raw = _pick_field(fields, "待测试 进入时间", "待测试进入时间", "status_time")
    ready_date = _date_yyyy_mm_dd(_as_str(ready_raw))
    expected_raw = _pick_field(
        fields, "开发排期 结束时间", "开发排期结束时间", "排期_结束时间"
    )
    expected = _max_date_from_value(expected_raw) if expected_raw is not None else ""
    ready = derive_ready(status)
    comment = derive_comment(ready_date, expected)
    simple = settings.feishu_simple_name
    return {
        "id": item_id,
        "name": name,
        "status": status,
        "ready": ready,
        "readyDate": ready_date,
        "expectedReadyDate": expected,
        "comment": comment,
        "url": f"https://project.feishu.cn/{simple}/userstory/detail/{item_id}",
    }


def map_bug(item: dict[str, Any]) -> dict[str, Any]:
    fields = _field_map(item)
    item_id = _as_str(_pick_field(fields, "Item Id", "item_id"))
    title = _as_str(_pick_field(fields, "Summary", "summary"))
    status = _norm_bug_status(_label(_pick_field(fields, "Status", "status")))
    priority = _label(_pick_field(fields, "Priority", "priority")).upper() or "P3"
    if priority not in {"P0", "P1", "P2", "P3"}:
        # sometimes "P1 · xxx"
        m = re.search(r"P[0-3]", priority)
        priority = m.group(0) if m else "P3"
    simple = settings.feishu_simple_name
    return {
        "id": item_id,
        "name": title,
        "summary": title,
        "status": status,
        "priority": priority,
        "reopenTimes": 0,
        "url": f"https://project.feishu.cn/{simple}/bug/detail/{item_id}",
    }


@contextmanager
def _refresh_lock() -> Iterator[None]:
    if not _THREAD_LOCK.acquire(blocking=False):
        raise FeishuRefreshBusy("飞书快照刷新进行中，请稍后再试")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            # Stale lock from crashed process (>30 min) → clear and retry once
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
                        "飞书快照刷新进行中（文件锁），请稍后再试"
                    ) from exc
            else:
                raise FeishuRefreshBusy("飞书快照刷新进行中（文件锁），请稍后再试")
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
        return {"ok": None, "message": "尚无刷新记录"}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"ok": None, "message": "状态文件损坏"}
    except (OSError, json.JSONDecodeError):
        return {"ok": None, "message": "状态文件不可读"}


def _write_status(payload: dict[str, Any]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def module_recency_key(name: str) -> str:
    dates = re.findall(r"(\d{8})", name or "")
    return max(dates) if dates else ""


def pick_latest_module_name(modules: list[dict[str, Any]]) -> str | None:
    if not modules:
        return None
    best = None
    best_key = ""
    for m in modules:
        name = str(m.get("name") or "")
        key = module_recency_key(name)
        if key and key >= best_key:
            best_key = key
            best = name
    if best:
        return best
    obis = [str(m.get("name") or "") for m in modules if re.match(r"^OBIS-", str(m.get("name") or ""), re.I)]
    if obis:
        return obis[-1]
    return str(modules[0].get("name") or "") or None


def resolve_scheduled_sprint() -> str:
    configured = (settings.feishu_snapshot_sprint or "").strip()
    if configured:
        return configured
    from .ms_client import MeterSphereClient

    name = pick_latest_module_name(MeterSphereClient().list_modules())
    if not name:
        raise FeishuRefreshConfigError("无法从 MeterSphere 解析最新 Module/Sprint")
    return name


def refresh_sprint(sprint: str, *, trigger: str = "manual") -> dict[str, Any]:
    sprint = (sprint or "").strip()
    if not sprint:
        raise ValueError("sprint 不能为空")
    if not (settings.mcp_user_token or "").strip():
        raise FeishuRefreshConfigError("未配置 MCP_USER_TOKEN，无法刷新飞书快照")

    started = now_beijing_iso()
    try:
        with _refresh_lock():
            logger.info(
                "feishu refresh start module=%s trigger=%s",
                sprint,
                trigger,
            )
            raw = fetch_sprint_raw(
                sprint, story_mql=STORY_MQL, bug_mql=BUG_MQL
            )
            feishu_sprint = str(raw["feishuSprint"])
            sprint_warning = raw.get("sprintResolveNote")
            expect_stories = int(raw["expectStories"])
            expect_bugs = int(raw["expectBugs"])
            stories = [map_story(x) for x in raw["stories"]]
            bugs = [map_bug(x) for x in raw["bugs"]]
            logger.info(
                "feishu refresh fetched module=%s feishu_sprint=%s",
                sprint,
                feishu_sprint,
            )
            fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = {
                "sprint": sprint,
                "feishuSprint": feishu_sprint,
                "source": "mcp",
                "fetchedAt": fetched_at,
                "projectKey": settings.feishu_project_key,
                "simpleName": settings.feishu_simple_name,
                "rules": SNAPSHOT_RULES,
                "stories": stories,
                "bugs": bugs,
            }
            if sprint_warning:
                payload["sprintResolveNote"] = sprint_warning
            validate = _load_validate_fn()
            errors = validate(
                payload, expect_stories=expect_stories, expect_bugs=expect_bugs
            )
            if errors:
                raise FeishuValidateError(errors)

            stamp = now_beijing().strftime("%Y%m%d_%H%M%S")
            archive = SNAPSHOT_DIR / f"{sprint}_{stamp}.json"
            latest = SNAPSHOT_DIR / f"{sprint}_latest.json"
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            archive.write_text(text, encoding="utf-8")
            tmp = latest.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(latest)

            ready_yes = sum(1 for s in stories if s.get("ready") == "Yes")
            delay = sum(1 for s in stories if s.get("comment") == "提测Delay")
            result = {
                "ok": True,
                "sprint": sprint,
                "feishuSprint": feishu_sprint,
                "sprintResolveNote": sprint_warning,
                "trigger": trigger,
                "startedAt": started,
                "finishedAt": now_beijing_iso(),
                "fetchedAt": fetched_at,
                "stories": len(stories),
                "bugs": len(bugs),
                "readyYes": ready_yes,
                "delay": delay,
                "archive": str(archive.relative_to(ROOT)).replace("\\", "/"),
                "latest": str(latest.relative_to(ROOT)).replace("\\", "/"),
            }
            _write_status(result)
            logger.info(
                "feishu refresh ok module=%s feishu_sprint=%s stories=%s bugs=%s",
                sprint,
                feishu_sprint,
                len(stories),
                len(bugs),
            )
            return result
    except Exception as exc:  # noqa: BLE001
        from .feishu_mcp import format_mcp_exception

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

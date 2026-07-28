# -*- coding: utf-8 -*-
"""JSON persistence for Feishu Bug quick-create templates."""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from .config import settings
from .timeutil import now_beijing_iso

STORE_PATH = settings.db_path.parent / "bug_templates.json"

_lock = threading.Lock()


def _empty_store() -> dict[str, Any]:
    return {"templates": []}


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_raw() -> dict[str, Any]:
    if not STORE_PATH.exists():
        return _empty_store()
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    templates = data.get("templates")
    if not isinstance(templates, list):
        templates = []
    return {"templates": [t for t in templates if isinstance(t, dict)]}


def list_templates() -> list[dict[str, Any]]:
    with _lock:
        return list(_load_raw()["templates"])


def get_template(template_id: str) -> dict[str, Any] | None:
    tid = (template_id or "").strip()
    if not tid:
        return None
    for item in list_templates():
        if str(item.get("id") or "") == tid:
            return item
    return None


def _normalize_person(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    user_key = str(raw.get("userKey") or raw.get("user_key") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not user_key:
        return None
    return {"userKey": user_key, "name": name or user_key}


def _normalize_people(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        person = _normalize_person(item)
        if not person or person["userKey"] in seen:
            continue
        seen.add(person["userKey"])
        out.append(person)
    return out


def _as_int_list(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _as_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def normalize_template(payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")

    base = dict(existing or {})
    fields_in = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    roles_in = payload.get("roles") if isinstance(payload.get("roles"), dict) else {}
    labels_in = payload.get("labels") if isinstance(payload.get("labels"), dict) else {}

    fields = {
        "summary": str(fields_in.get("summary") or ""),
        "description": str(fields_in.get("description") or ""),
        "sprintIds": _as_int_list(fields_in.get("sprintIds")),
        "issueTypeIds": _as_str_list(fields_in.get("issueTypeIds")),
        "executionMethodId": str(fields_in.get("executionMethodId") or "").strip(),
        "labelIds": _as_str_list(fields_in.get("labelIds")),
        "issueStageId": str(fields_in.get("issueStageId") or "").strip(),
        "bugEnvironmentId": str(fields_in.get("bugEnvironmentId") or "").strip(),
        "componentVersions": str(fields_in.get("componentVersions") or ""),
        "versionFoundIds": _as_int_list(fields_in.get("versionFoundIds")),
        "severityId": str(fields_in.get("severityId") or "").strip(),
        "priorityId": str(fields_in.get("priorityId") or "").strip(),
    }
    roles = {
        "devOwner": _normalize_people(roles_in.get("devOwner")),
        "qcOwner": _normalize_people(roles_in.get("qcOwner")),
        "reporter": _normalize_people(roles_in.get("reporter")),
    }
    labels = {
        "sprint": str(labels_in.get("sprint") or "").strip(),
        "versionFound": _as_str_list(labels_in.get("versionFound")),
    }

    tid = str(payload.get("id") or base.get("id") or "").strip()
    if not tid:
        tid = f"tpl_{uuid.uuid4().hex[:12]}"

    return {
        "id": tid,
        "name": name,
        "updatedAt": now_beijing_iso(),
        "projectKey": str(
            payload.get("projectKey")
            or base.get("projectKey")
            or settings.feishu_project_key
        ).strip(),
        "simpleName": str(
            payload.get("simpleName")
            or base.get("simpleName")
            or settings.feishu_simple_name
        ).strip()
        or "obis",
        "feishuTemplateId": str(
            payload.get("feishuTemplateId")
            or base.get("feishuTemplateId")
            or "4362903"
        ).strip(),
        "fields": fields,
        "roles": roles,
        "labels": labels,
    }


def create_template(payload: dict[str, Any]) -> dict[str, Any]:
    item = normalize_template(payload)
    with _lock:
        store = _load_raw()
        if any(str(t.get("id")) == item["id"] for t in store["templates"]):
            raise ValueError(f"模板 id 已存在: {item['id']}")
        names = {str(t.get("name") or "").strip().lower() for t in store["templates"]}
        if item["name"].lower() in names:
            raise ValueError(f"模板名称已存在: {item['name']}")
        store["templates"].append(item)
        _atomic_write(STORE_PATH, store)
    return item


def update_template(template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    tid = (template_id or "").strip()
    if not tid:
        raise ValueError("模板 id 不能为空")
    with _lock:
        store = _load_raw()
        idx = next(
            (i for i, t in enumerate(store["templates"]) if str(t.get("id")) == tid),
            None,
        )
        if idx is None:
            raise KeyError(tid)
        existing = store["templates"][idx]
        merged = dict(payload)
        merged["id"] = tid
        item = normalize_template(merged, existing=existing)
        for i, t in enumerate(store["templates"]):
            if i == idx:
                continue
            if str(t.get("name") or "").strip().lower() == item["name"].lower():
                raise ValueError(f"模板名称已存在: {item['name']}")
        store["templates"][idx] = item
        _atomic_write(STORE_PATH, store)
    return item


def delete_template(template_id: str) -> bool:
    tid = (template_id or "").strip()
    if not tid:
        return False
    with _lock:
        store = _load_raw()
        before = len(store["templates"])
        store["templates"] = [
            t for t in store["templates"] if str(t.get("id")) != tid
        ]
        if len(store["templates"]) == before:
            return False
        _atomic_write(STORE_PATH, store)
    return True


def _unique_copy_name(base_name: str, existing_names: set[str]) -> str:
    base = (base_name or "模板").strip() or "模板"
    candidate = f"{base}（副本）"
    if candidate.lower() not in existing_names:
        return candidate
    n = 2
    while True:
        candidate = f"{base}（副本{n}）"
        if candidate.lower() not in existing_names:
            return candidate
        n += 1


def copy_template(template_id: str) -> dict[str, Any]:
    """Duplicate a template with a new id and unique name."""
    tid = (template_id or "").strip()
    if not tid:
        raise ValueError("模板 id 不能为空")
    with _lock:
        store = _load_raw()
        src = next((t for t in store["templates"] if str(t.get("id")) == tid), None)
        if src is None:
            raise KeyError(tid)
        names = {str(t.get("name") or "").strip().lower() for t in store["templates"]}
        payload = {
            "name": _unique_copy_name(str(src.get("name") or ""), names),
            "projectKey": src.get("projectKey"),
            "simpleName": src.get("simpleName"),
            "feishuTemplateId": src.get("feishuTemplateId"),
            "fields": src.get("fields") if isinstance(src.get("fields"), dict) else {},
            "roles": src.get("roles") if isinstance(src.get("roles"), dict) else {},
            "labels": src.get("labels") if isinstance(src.get("labels"), dict) else {},
        }
        item = normalize_template(payload)
        store["templates"].append(item)
        _atomic_write(STORE_PATH, store)
    return item


def safe_filename_hint(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", (name or "").strip()) or "template"

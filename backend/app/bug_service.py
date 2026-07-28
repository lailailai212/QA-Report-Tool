# -*- coding: utf-8 -*-
"""Build create_workitem payloads and create Feishu Bugs."""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from .bug_meta import BUG_WORK_ITEM_TYPE, FIELD_KEYS
from .bug_template_store import get_template
from .config import settings
from .feishu_mcp import FeishuMcpError, create_workitem, upload_richtext_image

# MCP create_workitem role_owners uses role display names (see tool docs).
ROLE_NAMES = {
    "devOwner": "Dev Owner",
    "qcOwner": "QC Owner",
    "reporter": "Reporter",
}

MAX_IMAGES = 12
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _field(key: str, value: Any) -> dict[str, Any]:
    """MCP thrift requires field_value to be STRING (JSON-encode lists/dicts)."""
    if isinstance(value, (list, dict)):
        field_value: Any = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    elif value is None:
        field_value = ""
    else:
        field_value = str(value)
    return {"field_key": key, "field_value": field_value}


def _people_keys(people: list[dict[str, str]] | None) -> list[str]:
    out: list[str] = []
    for p in people or []:
        uk = str((p or {}).get("userKey") or "").strip()
        if uk:
            out.append(uk)
    return out


def _decode_image_payload(item: dict[str, Any]) -> tuple[str, str, bytes]:
    name = str(item.get("fileName") or item.get("filename") or "paste.png").strip()
    name = re.sub(r"[\\/]+", "_", name) or "paste.png"
    mime = str(item.get("mimeType") or item.get("mime") or "image/png").strip() or "image/png"

    raw_bytes = item.get("contentBytes")
    if isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        content = bytes(raw_bytes)
    else:
        raw_b64 = str(item.get("contentBase64") or item.get("base64") or "").strip()
        if not raw_b64:
            raise ValueError(f"图片 {name} 缺少内容")
        if "," in raw_b64 and raw_b64.lower().startswith("data:"):
            raw_b64 = raw_b64.split(",", 1)[1]
        try:
            content = base64.b64decode(raw_b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"图片 {name} Base64 解码失败") from exc

    if not content:
        raise ValueError(f"图片 {name} 内容为空")
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError(f"图片 {name} 超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 限制")
    return name, mime, content


def upload_description_images(
    images: list[dict[str, Any]] | None,
    *,
    project_key: str,
) -> list[dict[str, str]]:
    """Upload pasted images; return [{fileName, fileToken, fileUrl}, ...]."""
    if not images:
        return []
    if len(images) > MAX_IMAGES:
        raise ValueError(f"最多上传 {MAX_IMAGES} 张图片")
    uploaded: list[dict[str, str]] = []
    for idx, item in enumerate(images):
        if not isinstance(item, dict):
            continue
        name, mime, content = _decode_image_payload(item)
        if not name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
            # keep extension from mime
            ext = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(mime.lower(), ".png")
            if "." not in name:
                name = name + ext
        meta = upload_richtext_image(
            content=content,
            file_name=name,
            mime_type=mime,
            project_key=project_key,
            work_item_type=BUG_WORK_ITEM_TYPE,
            field_key=FIELD_KEYS["description"],
        )
        uploaded.append(
            {
                "fileName": name,
                "fileToken": meta.get("file_token") or "",
                "fileUrl": meta.get("file_url") or "",
                "index": str(idx + 1),
            }
        )
    return uploaded


def compose_description_with_inline_images(
    description: str, uploaded: list[dict[str, str]]
) -> str:
    """
    Replace {{IMG:N}} placeholders with markdown image refs in document order.
    If no placeholders exist, append images at the end (legacy).
    """
    text = description or ""

    def _md(item: dict[str, str]) -> str:
        name = item.get("fileName") or "image"
        url = (item.get("fileUrl") or "").strip()
        token = (item.get("fileToken") or "").strip()
        if not url and token:
            url = (
                f"{settings.feishu_mcp_domain}/goapi/v5/platform/file/stream/download/{token}"
            )
        if not url:
            return ""
        if token:
            return f'![{name}]({url} "token={token}")'
        return f"![{name}]({url})"

    if "{{IMG:" not in text:
        return append_images_to_description(text, uploaded)

    def repl(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        if idx < 0 or idx >= len(uploaded):
            return ""
        return _md(uploaded[idx])

    return re.sub(r"\{\{IMG:(\d+)\}\}", repl, text).strip()


def append_images_to_description(
    description: str, uploaded: list[dict[str, str]]
) -> str:
    """Append markdown image refs for Meego multi-text Description."""
    text = (description or "").rstrip()
    if not uploaded:
        return text
    parts = [text] if text else []
    parts.append("")
    for item in uploaded:
        name = item.get("fileName") or "image"
        url = (item.get("fileUrl") or "").strip()
        token = (item.get("fileToken") or "").strip()
        if not url and token:
            url = (
                f"{settings.feishu_mcp_domain}/goapi/v5/platform/file/stream/download/{token}"
            )
        if not url:
            continue
        if token:
            parts.append(f'![{name}]({url} "token={token}")')
        else:
            parts.append(f"![{name}]({url})")
    return "\n".join(parts).strip()


def build_create_fields(
    template: dict[str, Any],
    *,
    summary: str,
    description: str,
    severity_id: str,
    priority_id: str,
    component_versions: str,
) -> list[dict[str, Any]]:
    fields_cfg = template.get("fields") if isinstance(template.get("fields"), dict) else {}
    roles_cfg = template.get("roles") if isinstance(template.get("roles"), dict) else {}

    name = (summary or "").strip()
    if not name:
        raise ValueError("Summary 不能为空")
    sev = (severity_id or str(fields_cfg.get("severityId") or "")).strip()
    pri = (priority_id or str(fields_cfg.get("priorityId") or "")).strip()
    if not sev:
        raise ValueError("Severity 不能为空")
    if not pri:
        raise ValueError("Priority 不能为空")

    env = str(fields_cfg.get("bugEnvironmentId") or "").strip()
    stage = str(fields_cfg.get("issueStageId") or "").strip()
    exec_m = str(fields_cfg.get("executionMethodId") or "").strip()
    issue_types = [
        str(x).strip() for x in (fields_cfg.get("issueTypeIds") or []) if str(x).strip()
    ]
    version_ids = []
    for x in fields_cfg.get("versionFoundIds") or []:
        try:
            version_ids.append(int(x))
        except (TypeError, ValueError):
            continue
    sprint_ids = []
    for x in fields_cfg.get("sprintIds") or []:
        try:
            sprint_ids.append(int(x))
        except (TypeError, ValueError):
            continue
    label_ids = [
        str(x).strip() for x in (fields_cfg.get("labelIds") or []) if str(x).strip()
    ]
    comp = component_versions if component_versions is not None else fields_cfg.get(
        "componentVersions"
    )
    comp = str(comp or "")
    if not env:
        raise ValueError("模板缺少 Bug Environment")
    if not stage:
        raise ValueError("模板缺少 Issue Stage")
    if not exec_m:
        raise ValueError("模板缺少 Execution Method")
    if not issue_types:
        raise ValueError("模板缺少 Issue Type")
    if not version_ids:
        raise ValueError("模板缺少 Version Found")
    if not comp.strip():
        raise ValueError("Component Versions 不能为空")

    feishu_tpl = str(template.get("feishuTemplateId") or "4362903").strip()
    out: list[dict[str, Any]] = [
        _field(FIELD_KEYS["template"], feishu_tpl),
        _field(FIELD_KEYS["name"], name),
        _field(FIELD_KEYS["description"], description or ""),
        _field(FIELD_KEYS["priority"], pri),
        _field(FIELD_KEYS["severity"], sev),
        _field(FIELD_KEYS["bugEnvironment"], env),
        _field(FIELD_KEYS["issueStage"], stage),
        _field(FIELD_KEYS["executionMethod"], exec_m),
        _field(FIELD_KEYS["issueType"], [{"option_id": x} for x in issue_types]),
        _field(FIELD_KEYS["componentVersions"], comp),
        _field(FIELD_KEYS["versionFound"], version_ids),
    ]
    if sprint_ids:
        out.append(_field(FIELD_KEYS["sprint"], sprint_ids))
    if label_ids:
        out.append(_field(FIELD_KEYS["labels"], [{"option_id": x} for x in label_ids]))

    role_owners: list[dict[str, Any]] = []
    for role_key, role_name in ROLE_NAMES.items():
        owners = _people_keys(roles_cfg.get(role_key))
        if owners:
            role_owners.append({"role": role_name, "owners": owners})
    if role_owners:
        out.append(_field(FIELD_KEYS["roleOwners"], role_owners))
    return out


def create_bug_from_template(
    *,
    template_id: str,
    summary: str,
    description: str,
    severity_id: str,
    priority_id: str,
    component_versions: str,
    images: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    template = get_template(template_id)
    if not template:
        raise KeyError(template_id)
    project_key = str(template.get("projectKey") or settings.feishu_project_key).strip()
    simple = str(template.get("simpleName") or settings.feishu_simple_name).strip() or "obis"

    uploaded = upload_description_images(images, project_key=project_key)
    final_description = compose_description_with_inline_images(description, uploaded)

    fields = build_create_fields(
        template,
        summary=summary,
        description=final_description,
        severity_id=severity_id,
        priority_id=priority_id,
        component_versions=component_versions,
    )
    result = create_workitem(
        project_key=project_key,
        work_item_type=BUG_WORK_ITEM_TYPE,
        fields=fields,
    )
    wid = str(result.get("id") or "").strip()
    if not wid:
        raise FeishuMcpError(
            f"创建成功但未解析到 Bug ID，原始返回：{str(result.get('raw'))[:300]}"
        )
    url = f"{settings.feishu_mcp_domain}/{simple}/bug/detail/{wid}"
    return {
        "id": wid,
        "url": url,
        "summary": summary.strip(),
        "templateId": template_id,
        "templateName": template.get("name"),
        "uploadedImages": len(uploaded),
    }

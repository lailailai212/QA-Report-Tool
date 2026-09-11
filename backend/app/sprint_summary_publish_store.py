# -*- coding: utf-8 -*-
"""Persist published Sprint summary HTML for public share links."""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

from .config import settings
from .timeutil import now_beijing_iso

PUBLISH_DIR = settings.db_path.parent / "sprint_summary_published"
_LIVE_DETAILS_HREF = re.compile(
    r"/sprint-summary/details(?:\?sprint=[^\"'#\s]*)?",
    re.I,
)


def published_report_path(publish_id: str) -> str:
    return f"/published/sprint-summary/{publish_id}"


def published_details_path(publish_id: str) -> str:
    return f"{published_report_path(publish_id)}/details"


def rewrite_published_detail_links(html: str, publish_id: str) -> str:
    """Point live details links at the published details page (keep #anchors)."""
    dest = published_details_path(publish_id)
    return _LIVE_DETAILS_HREF.sub(dest, html)


def finalize_readonly_html(html: str) -> str:
    """Strip inline-edit markup so published pages cannot be edited in the browser."""
    out = html
    out = re.sub(r"\s*contenteditable\s*=\s*\"[^\"]*\"", "", out, flags=re.I)
    out = re.sub(r"\s*spellcheck\s*=\s*\"[^\"]*\"", "", out, flags=re.I)
    if "class=\"report-readonly\"" not in out and "class='report-readonly'" not in out:
        out = re.sub(r"<body(\s|>)", r"<body class=\"report-readonly\"\1", out, count=1, flags=re.I)
    return out


def new_publish_id() -> str:
    return secrets.token_urlsafe(12)


def _publish_dir(publish_id: str) -> Path:
    safe = (publish_id or "").strip().replace("/", "_")
    return PUBLISH_DIR / safe


def publish_html(
    sprint: str,
    html: str,
    *,
    title: str = "",
    details_html: str | None = None,
    publish_id: str | None = None,
) -> dict[str, Any]:
    publish_id = (publish_id or new_publish_id()).strip()
    d = _publish_dir(publish_id)
    d.mkdir(parents=True, exist_ok=False)
    report_html = rewrite_published_detail_links(finalize_readonly_html(html), publish_id)
    (d / "report.html").write_text(report_html, encoding="utf-8")
    if details_html:
        (d / "details.html").write_text(finalize_readonly_html(details_html), encoding="utf-8")
    meta = {
        "publishId": publish_id,
        "sprint": sprint,
        "title": title or f"Sprint 总结报告 · {sprint}",
        "publishedAt": now_beijing_iso(),
    }
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return meta


def load_published_html(publish_id: str) -> str | None:
    p = _publish_dir(publish_id) / "report.html"
    if not p.is_file():
        return None
    try:
        html = p.read_text(encoding="utf-8")
    except OSError:
        return None
    return rewrite_published_detail_links(finalize_readonly_html(html), publish_id)


def load_published_details(publish_id: str) -> str | None:
    p = _publish_dir(publish_id) / "details.html"
    if not p.is_file():
        return None
    try:
        html = p.read_text(encoding="utf-8")
    except OSError:
        return None
    return finalize_readonly_html(html)


def load_published_meta(publish_id: str) -> dict[str, Any] | None:
    p = _publish_dir(publish_id) / "meta.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

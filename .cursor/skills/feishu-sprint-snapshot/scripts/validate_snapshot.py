#!/usr/bin/env python3
"""Validate Feishu sprint snapshot JSON: no loss, unique ids, required fields."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# 与 backend/app/feishu_refresh.py SNAPSHOT_RULES / derive_ready 保持一致：
# 进入「待测试」及之后（含已验收、待闭环）均视为已提测。
DEFAULT_READY_YES = frozenset(
    {"待测试", "测试中", "待验收", "已验收", "待闭环", "已完成", "已关闭"}
)
STORY_REQUIRED = (
    "id",
    "name",
    "status",
    "ready",
    "readyDate",
    "expectedReadyDate",
    "comment",
    "url",
)
BUG_REQUIRED = (
    "id",
    "name",
    "summary",
    "status",
    "priority",
    "reopenTimes",
    "url",
)


def _parse_iso_date(value: Any) -> date | None:
    raw = str(value or "").strip()[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _default_ready_deadline(sprint: str) -> date | None:
    """Sprint 第二周第一个工作日（开始日 + 7 天后的首个工作日）。脚本自包含，不依赖 backend。"""
    found = re.findall(r"(\d{8})", sprint or "")
    if len(found) < 2:
        return None
    start = _parse_iso_date(f"{found[0][0:4]}-{found[0][4:6]}-{found[0][6:8]}")
    end = _parse_iso_date(f"{found[1][0:4]}-{found[1][4:6]}-{found[1][6:8]}")
    if not start:
        return None
    week2 = start + timedelta(days=7)
    last = end if end and end >= week2 else week2 + timedelta(days=6)
    cur = week2
    while cur <= last:
        if cur.weekday() < 5:
            return cur
        cur += timedelta(days=1)
    return None


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"OK: {msg}")


def _unique_ids(items: list[dict[str, Any]], label: str) -> list[str]:
    errors: list[str] = []
    ids = [str(x.get("id") or "") for x in items]
    if any(not i for i in ids):
        errors.append(f"{label}: empty id present")
    if len(ids) != len(set(ids)):
        errors.append(f"{label}: duplicate ids ({len(ids)} items, {len(set(ids))} unique)")
    return errors


def validate(
    data: dict[str, Any],
    *,
    expect_stories: int | None,
    expect_bugs: int | None,
) -> list[str]:
    errors: list[str] = []

    for key in ("sprint", "source", "fetchedAt", "projectKey", "simpleName", "rules"):
        if key not in data:
            errors.append(f"missing top-level key: {key}")

    stories = data.get("stories")
    bugs = data.get("bugs")
    if not isinstance(stories, list):
        errors.append("stories must be a list")
        stories = []
    if not isinstance(bugs, list):
        errors.append("bugs must be a list")
        bugs = []

    if expect_stories is not None and len(stories) != expect_stories:
        errors.append(
            f"stories count mismatch: file={len(stories)} expect={expect_stories}"
        )
    if expect_bugs is not None and len(bugs) != expect_bugs:
        errors.append(f"bugs count mismatch: file={len(bugs)} expect={expect_bugs}")

    errors.extend(_unique_ids(stories, "stories"))
    errors.extend(_unique_ids(bugs, "bugs"))

    rules = data.get("rules") if isinstance(data.get("rules"), dict) else {}
    listed = rules.get("readyYesStatuses")
    ready_yes = (
        frozenset(str(x).strip() for x in listed if str(x).strip())
        if isinstance(listed, list) and listed
        else DEFAULT_READY_YES
    )
    sprint_name = str(data.get("sprint") or data.get("feishuSprint") or "")
    sprint_deadline = _default_ready_deadline(sprint_name)
    sprint_dl = sprint_deadline.isoformat() if sprint_deadline else ""

    for i, s in enumerate(stories):
        if not isinstance(s, dict):
            errors.append(f"stories[{i}] not an object")
            continue
        for k in STORY_REQUIRED:
            if k not in s:
                errors.append(f"stories[{i}] id={s.get('id')}: missing {k}")
        status = str(s.get("status") or "")
        ready = s.get("ready")
        expect_ready = "Yes" if status in ready_yes else "No"
        if ready not in ("Yes", "No"):
            errors.append(f"stories[{i}] id={s.get('id')}: ready must be Yes/No")
        elif ready != expect_ready:
            errors.append(
                f"stories[{i}] id={s.get('id')}: ready={ready} but status={status!r} => {expect_ready}"
            )
        rd = str(s.get("readyDate") or "")[:10]
        stored_dl = str(s.get("readyDeadline") or "")[:10]
        dl = sprint_dl or stored_dl
        comment = str(s.get("comment") or "")
        expect_comment = "提测Delay" if rd and dl and rd > dl else ""
        if comment != expect_comment:
            errors.append(
                f"stories[{i}] id={s.get('id')}: comment={comment!r} expected {expect_comment!r} "
                f"(readyDate={rd or '-'} deadline={dl or '-'})"
            )
        if stored_dl and sprint_dl and stored_dl != sprint_dl:
            errors.append(
                f"stories[{i}] id={s.get('id')}: readyDeadline={stored_dl!r} expected {sprint_dl!r}"
            )

    for i, b in enumerate(bugs):
        if not isinstance(b, dict):
            errors.append(f"bugs[{i}] not an object")
            continue
        for k in BUG_REQUIRED:
            if k not in b:
                errors.append(f"bugs[{i}] id={b.get('id')}: missing {k}")
        name = (b.get("name") or "").strip()
        summary = (b.get("summary") or "").strip()
        if not name and not summary:
            errors.append(f"bugs[{i}] id={b.get('id')}: empty name/summary")
        elif name and summary and name != summary:
            errors.append(
                f"bugs[{i}] id={b.get('id')}: name/summary mismatch"
            )
        if int(b.get("reopenTimes") or 0) != 0:
            errors.append(
                f"bugs[{i}] id={b.get('id')}: reopenTimes must be 0 (MCP window limit)"
            )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="snapshot json path")
    parser.add_argument("--expect-stories", type=int, default=None)
    parser.add_argument("--expect-bugs", type=int, default=None)
    args = parser.parse_args()

    if not args.path.is_file():
        _fail(f"file not found: {args.path}")
        return 2

    try:
        data = json.loads(args.path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"invalid json: {exc}")
        return 2

    if not isinstance(data, dict):
        _fail("root must be object")
        return 2

    errors = validate(
        data,
        expect_stories=args.expect_stories,
        expect_bugs=args.expect_bugs,
    )
    if errors:
        for e in errors:
            _fail(e)
        print(
            f"SUMMARY: {len(errors)} error(s); "
            f"stories={len(data.get('stories') or [])} bugs={len(data.get('bugs') or [])}",
            file=sys.stderr,
        )
        return 1

    stories = data.get("stories") or []
    bugs = data.get("bugs") or []
    ready = sum(1 for s in stories if s.get("ready") == "Yes")
    delay = sum(1 for s in stories if s.get("comment") == "提测Delay")
    _ok(f"{args.path}")
    _ok(f"stories={len(stories)} bugs={len(bugs)} readyYes={ready} delay={delay}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

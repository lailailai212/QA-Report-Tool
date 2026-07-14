#!/usr/bin/env python3
"""Validate Feishu sprint snapshot JSON: no loss, unique ids, required fields."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

READY_YES = frozenset({"待测试", "测试中", "待验收"})
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

    for i, s in enumerate(stories):
        if not isinstance(s, dict):
            errors.append(f"stories[{i}] not an object")
            continue
        for k in STORY_REQUIRED:
            if k not in s:
                errors.append(f"stories[{i}] id={s.get('id')}: missing {k}")
        status = str(s.get("status") or "")
        ready = s.get("ready")
        expect_ready = "Yes" if status in READY_YES else "No"
        if ready not in ("Yes", "No"):
            errors.append(f"stories[{i}] id={s.get('id')}: ready must be Yes/No")
        elif ready != expect_ready:
            errors.append(
                f"stories[{i}] id={s.get('id')}: ready={ready} but status={status!r} => {expect_ready}"
            )
        rd = str(s.get("readyDate") or "")
        ed = str(s.get("expectedReadyDate") or "")
        comment = str(s.get("comment") or "")
        expect_comment = "提测Delay" if rd and ed and rd > ed else ""
        if comment != expect_comment:
            errors.append(
                f"stories[{i}] id={s.get('id')}: comment={comment!r} expected {expect_comment!r}"
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

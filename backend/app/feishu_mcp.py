# -*- coding: utf-8 -*-
"""Call Feishu Project MCP search_by_mql without LLM / Cursor UI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator, TextIO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import settings

logger = logging.getLogger(__name__)

MAX_PAGES = 100

# @lark-project/mcp prints this on normal stdin-close shutdown; not a real failure.
_MCP_STDERR_NOISE = re.compile(r"MCP Server running on stdio error", re.I)


class FeishuMcpError(RuntimeError):
    pass


def _npx_command() -> str:
    override = os.environ.get("FEISHU_MCP_COMMAND")
    if override:
        return override
    # Windows often needs npx.cmd for CreateProcess without shell
    if os.name == "nt":
        return shutil.which("npx.cmd") or shutil.which("npx") or "npx.cmd"
    return shutil.which("npx") or "npx"


def _mcp_child_env(token: str) -> dict[str, str]:
    """
    Env for Feishu MCP subprocess.

    dotenv@17+ prints 'injected env ...' to stdout by default, which corrupts
    MCP JSON-RPC over stdio. DOTENV_CONFIG_QUIET=true suppresses that.
    """
    env = {**os.environ, "MCP_USER_TOKEN": token, "DOTENV_CONFIG_QUIET": "true"}
    env.pop("DOTENV_CONFIG_DEBUG", None)
    return env


def _server_params() -> StdioServerParameters:
    token = (settings.mcp_user_token or "").strip()
    if not token:
        raise FeishuMcpError("未配置 MCP_USER_TOKEN，无法刷新飞书快照")
    command = _npx_command()
    args = [
        "-y",
        "@lark-project/mcp",
        "--domain",
        settings.feishu_mcp_domain,
    ]
    # cwd away from project .env is belt-and-suspenders; quiet is the real fix
    cwd = os.environ.get("TEMP") or os.environ.get("TMP") or None
    return StdioServerParameters(
        command=command,
        args=args,
        env=_mcp_child_env(token),
        cwd=cwd,
    )


@contextmanager
def _mcp_errlog() -> Iterator[TextIO]:
    """
    Capture MCP child stderr to a real file (needs fileno on Windows), then
    re-log useful lines and drop known teardown noise.
    """
    fd, path = tempfile.mkstemp(prefix="qa-mcp-stderr-", suffix=".log")
    os.close(fd)
    try:
        with open(path, "w+", encoding="utf-8", errors="replace") as fh:
            yield fh
            try:
                fh.flush()
                fh.seek(0)
                blob = fh.read()
            except OSError:
                blob = ""
        for raw in blob.splitlines():
            line = raw.strip()
            if not line:
                continue
            if _MCP_STDERR_NOISE.search(line):
                logger.debug("mcp stderr ignored: %s", line)
                continue
            logger.warning("mcp stderr: %s", line)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@asynccontextmanager
async def _mcp_session() -> AsyncIterator[ClientSession]:
    params = _server_params()
    with _mcp_errlog() as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def root_exception(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroup / TaskGroup wrappers to the actionable cause."""
    cur: BaseException = exc
    seen: set[int] = set()
    while id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, BaseExceptionGroup):
            preferred = None
            for sub in cur.exceptions:
                if isinstance(sub, FeishuMcpError):
                    preferred = sub
                    break
            cur = preferred or cur.exceptions[0]
            continue
        cause = cur.__cause__ or cur.__context__
        if cause is not None and (
            "TaskGroup" in type(cur).__name__
            or "unhandled errors in a TaskGroup" in str(cur)
        ):
            cur = cause
            continue
        break
    return cur


def format_mcp_exception(exc: BaseException) -> str:
    root = root_exception(exc)
    if isinstance(root, FeishuMcpError):
        return str(root)
    if isinstance(root, BaseExceptionGroup):
        parts = [format_mcp_exception(e) for e in root.exceptions]
        return "; ".join(parts)
    msg = str(root).strip() or type(root).__name__
    if "TaskGroup" in msg or "unhandled errors" in msg:
        # last resort: dig nested exceptions attribute
        nested = getattr(root, "exceptions", None)
        if nested:
            return format_mcp_exception(nested[0])
    return msg


def _tool_error_text(result: Any) -> str:
    texts: list[str] = []
    for block in result.content or []:
        t = getattr(block, "text", None)
        if t:
            texts.append(str(t))
    return "\n".join(texts)


def _friendly_tool_error(blob: str) -> str:
    if "attrValueLabel not found" in blob and "Sprint" in blob:
        m = re.search(r"attrValueLabelUUID:([^\s|,]+)", blob)
        sprint = m.group(1) if m else ""
        # also try value=OBIS-...
        if not sprint:
            m2 = re.search(r"value=(OBIS-[^\s\]]+)", blob)
            sprint = m2.group(1) if m2 else ""
        label = sprint or "当前 Sprint"
        return (
            f"飞书侧找不到 Sprint「{label}」的枚举值（metadata attrValueLabel not found）。"
            "请确认该 Sprint 在飞书项目中仍存在且名称完全一致；"
            "若已改名/归档，请改选有效 Module 后再刷新。"
        )
    if "rate" in blob.lower() and ("limit" in blob.lower() or "限流" in blob):
        return f"飞书 MCP 可能触发限流：{blob[:300]}"
    # keep concise
    compact = re.sub(r"\s+", " ", blob).strip()
    if len(compact) > 400:
        compact = compact[:400] + "…"
    return f"search_by_mql 失败：{compact}"


def _parse_tool_json(result: Any, *, tool_name: str = "search_by_mql") -> dict[str, Any]:
    if getattr(result, "isError", False):
        blob = _tool_error_text(result)
        if tool_name == "search_by_mql":
            raise FeishuMcpError(_friendly_tool_error(blob or str(result)))
        compact = re.sub(r"\s+", " ", (blob or str(result))).strip()
        if len(compact) > 400:
            compact = compact[:400] + "…"
        raise FeishuMcpError(f"{tool_name} 失败：{compact}")
    texts: list[str] = []
    for block in result.content or []:
        t = getattr(block, "text", None)
        if t:
            texts.append(t)
    blob = "\n".join(texts)
    if not blob:
        raise FeishuMcpError(f"{tool_name} 返回空内容")
    # Prefer object; fall back to JSON array wrapped as {"list": ...}
    start_obj = blob.find("{")
    end_obj = blob.rfind("}")
    start_arr = blob.find("[")
    end_arr = blob.rfind("]")
    if start_obj >= 0 and end_obj > start_obj and (
        start_arr < 0 or start_obj <= start_arr
    ):
        snippet = blob[start_obj : end_obj + 1]
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError as exc:
            raise FeishuMcpError(f"{tool_name} JSON 解析失败: {exc}") from exc
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return {"list": data}
    if start_arr >= 0 and end_arr > start_arr:
        snippet = blob[start_arr : end_arr + 1]
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError as exc:
            raise FeishuMcpError(f"{tool_name} JSON 解析失败: {exc}") from exc
        if isinstance(data, list):
            return {"list": data}
        if isinstance(data, dict):
            return data
    raise FeishuMcpError(f"{tool_name} 无 JSON 对象: {blob[:200]!r}")


async def _call_tool(
    session: ClientSession, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await session.call_tool(tool_name, arguments=arguments)
    return _parse_tool_json(result, tool_name=tool_name)


def _run_coro(coro_factory):  # type: ignore[no-untyped-def]
    """Run async factory in sync context (FastAPI sync routes / threads)."""

    async def _run():
        return await coro_factory()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_run())).result()


def _page_items(data: dict[str, Any], group_id: str) -> list[dict[str, Any]]:
    raw = (data.get("data") or {}).get(group_id) or []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


async def _search_by_mql(
    session: ClientSession,
    *,
    mql: str | None = None,
    session_id: str | None = None,
    group_pagination_list: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"project_key": settings.feishu_project_key}
    if session_id:
        arguments["session_id"] = session_id
        if group_pagination_list is not None:
            arguments["group_pagination_list"] = group_pagination_list
        # Skill: when paging, mql empty / omit
        if mql:
            arguments["mql"] = mql
    else:
        if not mql:
            raise FeishuMcpError("首次 search_by_mql 必须传 mql")
        arguments["mql"] = mql
    result = await session.call_tool("search_by_mql", arguments=arguments)
    return _parse_tool_json(result)


async def _fetch_all_mql_on_session(
    session: ClientSession, mql: str
) -> tuple[list[dict[str, Any]], int]:
    """Paginate until collected == list[0].count. Returns (items, expected_count)."""
    first = await _search_by_mql(session, mql=mql)
    lst = first.get("list") or []
    if not lst or not isinstance(lst[0], dict):
        raise FeishuMcpError("search_by_mql 缺少 list[0]")
    meta = lst[0]
    expected = int(meta.get("count") or 0)
    session_id = first.get("session_id")
    if not session_id:
        raise FeishuMcpError("search_by_mql 缺少 session_id")
    group_infos = meta.get("group_infos") or []
    if group_infos and isinstance(group_infos[0], dict):
        group_id = str(group_infos[0].get("group_id") or "1")
    else:
        group_id = "1"

    items = _page_items(first, group_id)
    page_num = 2
    while len(items) < expected:
        if page_num > MAX_PAGES:
            raise FeishuMcpError(
                f"分页超过 {MAX_PAGES} 页仍未齐: "
                f"collected={len(items)} expect={expected}"
            )
        page = await _search_by_mql(
            session,
            session_id=session_id,
            group_pagination_list=[
                {"group_id": group_id, "page_num": page_num}
            ],
        )
        chunk = _page_items(page, group_id)
        if not chunk and len(items) < expected:
            raise FeishuMcpError(
                f"丢页: page_num={page_num} 空页且 "
                f"collected={len(items)} < {expected}"
            )
        items.extend(chunk)
        page_num += 1

    if len(items) != expected:
        raise FeishuMcpError(
            f"条数未对齐: collected={len(items)} expect={expected}"
        )
    logger.info("fetch_all_mql ok: expected=%s pages=%s", expected, page_num - 1)
    return items, expected


async def fetch_all_mql_async(mql: str) -> tuple[list[dict[str, Any]], int]:
    try:
        async with _mcp_session() as session:
            return await _fetch_all_mql_on_session(session, mql)
    except FeishuMcpError:
        raise
    except BaseExceptionGroup as eg:
        root = root_exception(eg)
        if isinstance(root, FeishuMcpError):
            raise root from None
        raise FeishuMcpError(format_mcp_exception(eg)) from eg
    except Exception as exc:  # noqa: BLE001
        root = root_exception(exc)
        if isinstance(root, FeishuMcpError):
            raise root from None
        raise FeishuMcpError(format_mcp_exception(exc)) from exc


def fetch_all_mql(mql: str) -> tuple[list[dict[str, Any]], int]:
    """Sync wrapper safe to call from FastAPI sync routes / APScheduler threads."""

    async def _run() -> tuple[list[dict[str, Any]], int]:
        return await fetch_all_mql_async(mql)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(_run())
        except BaseExceptionGroup as eg:
            root = root_exception(eg)
            if isinstance(root, FeishuMcpError):
                raise root from None
            raise FeishuMcpError(format_mcp_exception(eg)) from eg

    # Running loop in this thread: offload to a fresh loop in a worker thread
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        try:
            return pool.submit(lambda: asyncio.run(_run())).result()
        except BaseExceptionGroup as eg:
            root = root_exception(eg)
            if isinstance(root, FeishuMcpError):
                raise root from None
            raise FeishuMcpError(format_mcp_exception(eg)) from eg
        except Exception as exc:  # noqa: BLE001
            root = root_exception(exc)
            if isinstance(root, FeishuMcpError):
                raise root from None
            raise


def _items_to_sprint_rows(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse MQL rows into [{id, name}] for Sprint / Version pickers."""
    out: list[dict[str, str]] = []
    for item in items:
        by_name: dict[str, Any] = {}
        by_key: dict[str, Any] = {}
        for f in item.get("moql_field_list") or []:
            if not isinstance(f, dict):
                continue
            val = f.get("value")
            if f.get("name"):
                by_name[str(f["name"])] = val
            if f.get("key"):
                by_key[str(f["key"])] = val

        sid = ""
        for key in ("Item Id", "工作项id", "work_item_id"):
            iv = by_name.get(key) if key in by_name else by_key.get(key)
            if isinstance(iv, dict) and iv.get("long_value") is not None:
                sid = str(iv["long_value"])
                break
            if isinstance(iv, dict) and iv.get("number_value") is not None:
                sid = str(int(iv["number_value"]))
                break

        name = ""
        for key in ("Summary", "Version", "name", "Name"):
            sv = by_name.get(key) if key in by_name else by_key.get(key)
            if isinstance(sv, dict) and sv.get("string_value") is not None:
                name = str(sv["string_value"]).strip()
            elif isinstance(sv, str):
                name = sv.strip()
            if name:
                break

        if name:
            out.append({"id": sid, "name": name})
    return out


async def _list_sprint_names_on_session(session: ClientSession) -> list[dict[str, str]]:
    mql = "SELECT `Item Id`, `Summary` FROM `OBIS`.`sprint`"
    first = await _search_by_mql(session, mql=mql)
    lst = first.get("list") or []
    if not lst or not isinstance(lst[0], dict):
        return []
    expected = int(lst[0].get("count") or 0)
    session_id = first.get("session_id")
    group_infos = lst[0].get("group_infos") or []
    group_id = "1"
    if group_infos and isinstance(group_infos[0], dict):
        group_id = str(group_infos[0].get("group_id") or "1")
    items = _page_items(first, group_id)
    page_num = 2
    # Sprint list is small; still paginate to be safe (cap 5 pages)
    while session_id and len(items) < expected and page_num <= 5:
        page = await _search_by_mql(
            session,
            session_id=session_id,
            group_pagination_list=[
                {"group_id": group_id, "page_num": page_num}
            ],
        )
        chunk = _page_items(page, group_id)
        if not chunk:
            break
        items.extend(chunk)
        page_num += 1
    return _items_to_sprint_rows(items)


async def list_sprint_names_async() -> list[dict[str, str]]:
    """List Feishu Sprint work items: [{id, name}, ...] (newest first as returned)."""
    try:
        async with _mcp_session() as session:
            return await _list_sprint_names_on_session(session)
    except Exception as exc:  # noqa: BLE001
        raise FeishuMcpError(f"拉取 Sprint 列表失败: {format_mcp_exception(exc)}") from exc


def list_sprint_names() -> list[dict[str, str]]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(list_sprint_names_async())
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(list_sprint_names_async())).result()


def _resolve_from_names(
    requested: str, sprints: list[dict[str, str]]
) -> tuple[str, str | None]:
    names = [s["name"] for s in sprints if s.get("name")]
    if requested in names:
        return requested, None

    # OBIS-YYYYMMDD-YYYYMMDD → match same start date
    m = re.match(r"^(OBIS-\d{8})-(\d{8})$", requested, re.I)
    if m:
        prefix = m.group(1) + "-"
        candidates = [n for n in names if n.upper().startswith(prefix.upper())]
        if len(candidates) == 1:
            resolved = candidates[0]
            warn = (
                f"飞书 Sprint 已更名为「{resolved}」（原请求「{requested}」）；"
                f"已按新名称拉取，快照仍写入 Module 名 {requested}"
            )
            return resolved, warn
        if len(candidates) > 1:
            raise FeishuMcpError(
                f"飞书侧找不到 Sprint「{requested}」，同起始日候选有多条："
                f"{', '.join(candidates)}。请改选准确 Module/Sprint 后再刷新。"
            )

    # fuzzy: contain requested or requested contain name
    loose = [n for n in names if requested in n or n in requested]
    if len(loose) == 1:
        return loose[0], (
            f"飞书 Sprint 匹配为「{loose[0]}」（原请求「{requested}」）；"
            f"快照仍写入 Module 名 {requested}"
        )

    sample = ", ".join(names[:8]) if names else "(空)"
    raise FeishuMcpError(
        f"飞书侧找不到 Sprint「{requested}」。"
        f"请确认飞书迭代名称；近期 Sprint 示例：{sample}"
    )


def resolve_sprint_label(requested: str) -> tuple[str, str | None]:
    """
    Map Module/Sprint name to a Feishu Sprint label usable in MQL.

    Feishu may rename Sprint end-dates (e.g. OBIS-20260706-20260717 →
    OBIS-20260706-20260724). Exact match wins; else unique same-start-date match.
    """
    requested = (requested or "").strip()
    if not requested:
        raise FeishuMcpError("sprint 不能为空")
    return _resolve_from_names(requested, list_sprint_names())


async def fetch_sprint_raw_async(
    module_sprint: str, *, story_mql: str, bug_mql: str
) -> dict[str, Any]:
    """
    One MCP process: resolve Sprint label, then pull stories + bugs.

    Returns keys: feishuSprint, sprintResolveNote, stories, expectStories,
    bugs, expectBugs.
    """
    requested = (module_sprint or "").strip()
    if not requested:
        raise FeishuMcpError("sprint 不能为空")
    try:
        async with _mcp_session() as session:
            sprints = await _list_sprint_names_on_session(session)
            feishu_sprint, note = _resolve_from_names(requested, sprints)
            stories, expect_stories = await _fetch_all_mql_on_session(
                session, story_mql.format(sprint=feishu_sprint)
            )
            bugs, expect_bugs = await _fetch_all_mql_on_session(
                session, bug_mql.format(sprint=feishu_sprint)
            )
            return {
                "feishuSprint": feishu_sprint,
                "sprintResolveNote": note,
                "stories": stories,
                "expectStories": expect_stories,
                "bugs": bugs,
                "expectBugs": expect_bugs,
            }
    except FeishuMcpError:
        raise
    except BaseExceptionGroup as eg:
        root = root_exception(eg)
        if isinstance(root, FeishuMcpError):
            raise root from None
        raise FeishuMcpError(format_mcp_exception(eg)) from eg
    except Exception as exc:  # noqa: BLE001
        root = root_exception(exc)
        if isinstance(root, FeishuMcpError):
            raise root from None
        raise FeishuMcpError(format_mcp_exception(exc)) from exc


def fetch_sprint_raw(
    module_sprint: str, *, story_mql: str, bug_mql: str
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        return await fetch_sprint_raw_async(
            module_sprint, story_mql=story_mql, bug_mql=bug_mql
        )

    return _run_coro(_run)


def _extract_work_item_id(data: dict[str, Any]) -> str:
    for key in ("id", "work_item_id", "workItemId", "item_id"):
        val = data.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    nested = data.get("data")
    if isinstance(nested, dict):
        for key in ("id", "work_item_id", "workItemId"):
            val = nested.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    lst = data.get("list")
    if isinstance(lst, list) and lst and isinstance(lst[0], dict):
        return _extract_work_item_id(lst[0])
    return ""


async def create_workitem_async(
    *,
    project_key: str,
    work_item_type: str,
    fields: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a Feishu work item via MCP create_workitem."""
    # Thrift schema: each field_value must be STRING (JSON for complex types).
    normalized: list[dict[str, Any]] = []
    for item in fields:
        if not isinstance(item, dict):
            continue
        key = item.get("field_key")
        value = item.get("field_value")
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        elif value is None:
            value = ""
        else:
            value = str(value)
        normalized.append({"field_key": key, "field_value": value})
    arguments = {
        "project_key": project_key,
        "work_item_type": work_item_type,
        "fields": normalized,
    }
    try:
        async with _mcp_session() as session:
            data = await _call_tool(session, "create_workitem", arguments)
            wid = _extract_work_item_id(data)
            return {"raw": data, "id": wid}
    except FeishuMcpError:
        raise
    except BaseExceptionGroup as eg:
        root = root_exception(eg)
        if isinstance(root, FeishuMcpError):
            raise root from None
        raise FeishuMcpError(format_mcp_exception(eg)) from eg
    except Exception as exc:  # noqa: BLE001
        root = root_exception(exc)
        if isinstance(root, FeishuMcpError):
            raise root from None
        raise FeishuMcpError(format_mcp_exception(exc)) from exc


def create_workitem(
    *,
    project_key: str,
    work_item_type: str,
    fields: list[dict[str, Any]],
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        return await create_workitem_async(
            project_key=project_key,
            work_item_type=work_item_type,
            fields=fields,
        )

    return _run_coro(_run)


async def search_users_async(
    queries: list[str], *, project_key: str | None = None
) -> list[dict[str, str]]:
    keys = [str(q).strip() for q in queries if str(q).strip()][:20]
    if not keys:
        return []
    arguments: dict[str, Any] = {"user_keys": keys}
    pk = (project_key or settings.feishu_project_key or "").strip()
    if pk:
        arguments["project_key"] = pk
    try:
        async with _mcp_session() as session:
            data = await _call_tool(session, "search_user_info", arguments)
    except FeishuMcpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeishuMcpError(f"search_user_info 失败: {format_mcp_exception(exc)}") from exc

    raw_list = data.get("list") or data.get("data") or data.get("users") or []
    if isinstance(data.get("data"), dict):
        nested = data["data"]
        raw_list = nested.get("list") or nested.get("users") or raw_list
    if not isinstance(raw_list, list):
        raw_list = []

    out: list[dict[str, str]] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        user_key = str(
            item.get("user_key")
            or item.get("userKey")
            or item.get("user_id")
            or item.get("id")
            or ""
        ).strip()
        name = str(
            item.get("name_cn")
            or item.get("name")
            or item.get("username")
            or item.get("email")
            or ""
        ).strip()
        email = str(item.get("email") or "").strip()
        if not user_key:
            continue
        label = name or email or user_key
        if email and name and email not in label:
            label = f"{name} <{email}>"
        out.append({"userKey": user_key, "name": label, "email": email})
    return out


def search_users(queries: list[str], *, project_key: str | None = None) -> list[dict[str, str]]:
    async def _run() -> list[dict[str, str]]:
        return await search_users_async(queries, project_key=project_key)

    return _run_coro(_run)


async def list_versions_async(query: str = "") -> list[dict[str, str]]:
    """List Version work items for Version Found picker (first page).

    Version type uses `work_item_id` + `name` (not Item Id / Summary).
    """
    q = (query or "").strip().replace("'", "")
    if q:
        mql = (
            "SELECT `work_item_id`, `name` FROM `OBIS`.`Version` "
            f"WHERE `name` like '%{q}%'"
        )
    else:
        mql = "SELECT `work_item_id`, `name` FROM `OBIS`.`Version`"
    try:
        async with _mcp_session() as session:
            first = await _search_by_mql(session, mql=mql)
            lst = first.get("list") or []
            if not lst or not isinstance(lst[0], dict):
                return []
            group_infos = lst[0].get("group_infos") or []
            group_id = "1"
            if group_infos and isinstance(group_infos[0], dict):
                group_id = str(group_infos[0].get("group_id") or "1")
            items = _page_items(first, group_id)[:50]
            return _items_to_sprint_rows(items)
    except FeishuMcpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeishuMcpError(f"拉取 Version 列表失败: {format_mcp_exception(exc)}") from exc


def list_versions(query: str = "") -> list[dict[str, str]]:
    async def _run() -> list[dict[str, str]]:
        return await list_versions_async(query)

    return _run_coro(_run)


def _parse_upload_http_body(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeishuMcpError(f"上传响应非 JSON: {text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise FeishuMcpError(f"上传响应格式异常: {text[:200]!r}")
    code = data.get("code")
    if code not in (0, "0", None):
        raise FeishuMcpError(
            f"文件上传失败: code={code} message={data.get('message') or text[:200]}"
        )
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    token = str(
        payload.get("file_token") or payload.get("token") or payload.get("fileToken") or ""
    ).strip()
    url = str(
        payload.get("file_url") or payload.get("url") or payload.get("fileUrl") or ""
    ).strip()
    if not token and not url:
        raise FeishuMcpError(f"上传成功但未返回 file_token/url: {text[:300]}")
    if not url and token:
        url = (
            f"{settings.feishu_mcp_domain}/goapi/v5/platform/file/stream/download/{token}"
        )
    return {"file_token": token, "file_url": url}


def _http_put_file_bytes(
    *,
    upload_url: str,
    sign: str,
    content: bytes,
    mime_type: str,
    is_multipart: bool,
) -> dict[str, str]:
    import requests

    headers_base = {"X-Meego-File-Sign": sign, "Content-Type": mime_type or "application/octet-stream"}
    if not is_multipart:
        url = (upload_url or "").replace(":part_number", "0")
        resp = requests.post(url, data=content, headers=headers_base, timeout=120)
        if resp.status_code >= 400:
            raise FeishuMcpError(
                f"文件上传 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return _parse_upload_http_body(resp.text)

    # Multipart: upload chunks sequentially; last response carries token/url.
    chunk_size = 4 * 1024 * 1024
    total = len(content)
    last_text = ""
    part_index = 0
    offset = 0
    while offset < total:
        end = min(offset + chunk_size, total)
        chunk = content[offset:end]
        url = (upload_url or "").replace(":part_number", str(part_index))
        # Some gateways also expect range headers; keep Content-Type aligned with mime.
        resp = requests.post(url, data=chunk, headers=headers_base, timeout=180)
        if resp.status_code >= 400:
            raise FeishuMcpError(
                f"分片上传 HTTP {resp.status_code} part={part_index}: {resp.text[:300]}"
            )
        last_text = resp.text
        part_index += 1
        offset = end
    return _parse_upload_http_body(last_text)


async def upload_richtext_image_async(
    *,
    content: bytes,
    file_name: str,
    mime_type: str,
    project_key: str | None = None,
    work_item_type: str | None = None,
    field_key: str = "description",
) -> dict[str, str]:
    """Upload an image for Bug Description (resource_type=16). Returns file_token/file_url."""
    if not content:
        raise FeishuMcpError("图片内容为空")
    name = (file_name or "paste.png").strip() or "paste.png"
    mime = (mime_type or "image/png").strip() or "image/png"
    pk = (project_key or settings.feishu_project_key or "").strip()
    wit = (work_item_type or "67e61b9027f30a82566ed1cc").strip()
    arguments = {
        "project_key": pk,
        "work_item_type": wit,
        "field_key": field_key or "description",
        "file_name": name,
        "mime_type": mime,
        "resource_type": 16,
        "size": len(content),
    }
    try:
        async with _mcp_session() as session:
            cred = await _call_tool(session, "upload_file", arguments)
    except FeishuMcpError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise FeishuMcpError(f"upload_file 失败: {format_mcp_exception(exc)}") from exc

    upload_url = str(cred.get("upload_url") or "").strip()
    sign = str(cred.get("sign") or "").strip()
    if not upload_url or not sign:
        raise FeishuMcpError(f"upload_file 未返回 upload_url/sign: {cred}")
    is_multipart = bool(cred.get("is_multipart"))
    return _http_put_file_bytes(
        upload_url=upload_url,
        sign=sign,
        content=content,
        mime_type=mime,
        is_multipart=is_multipart,
    )


def upload_richtext_image(
    *,
    content: bytes,
    file_name: str,
    mime_type: str,
    project_key: str | None = None,
    work_item_type: str | None = None,
    field_key: str = "description",
) -> dict[str, str]:
    async def _run() -> dict[str, str]:
        return await upload_richtext_image_async(
            content=content,
            file_name=file_name,
            mime_type=mime_type,
            project_key=project_key,
            work_item_type=work_item_type,
            field_key=field_key,
        )

    return _run_coro(_run)

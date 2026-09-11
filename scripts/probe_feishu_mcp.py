"""Probe: call Feishu Project MCP search_by_mql without LLM/Cursor."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def load_feishu_mcp_config() -> tuple[str, list[str], dict[str, str]]:
    """Read FeishuProjectMcp launch info from ~/.cursor/mcp.json (comment-tolerant)."""
    import re

    mcp_json = Path.home() / ".cursor" / "mcp.json"
    raw = mcp_json.read_text(encoding="utf-8")

    token = os.environ.get("MCP_USER_TOKEN")
    if not token:
        m = re.search(r'"MCP_USER_TOKEN"\s*:\s*"([^"]+)"', raw)
        if not m:
            raise SystemExit("MCP_USER_TOKEN not found in env or mcp.json")
        token = m.group(1)

    # Prefer known Feishu MCP package args from user config
    command = "npx"
    args = [
        "-y",
        "@lark-project/mcp",
        "--domain",
        "https://project.feishu.cn",
    ]
    # Allow override via env
    if os.environ.get("FEISHU_MCP_COMMAND"):
        command = os.environ["FEISHU_MCP_COMMAND"]
    # dotenv@17 logs to stdout by default and breaks MCP JSON-RPC
    env = {
        **os.environ,
        "MCP_USER_TOKEN": token,
        "DOTENV_CONFIG_QUIET": "true",
    }
    env.pop("DOTENV_CONFIG_DEBUG", None)
    return command, args, env


async def main() -> int:
    command, args, env = load_feishu_mcp_config()
    print(f"launch: {command} {' '.join(args)}")
    print(f"token set: {bool(env.get('MCP_USER_TOKEN'))}")

    params = StdioServerParameters(command=command, args=args, env=env)
    mql = (
        "SELECT `Item Id`, `Summary`, `Status` "
        "FROM `OBIS`.`User Story` "
        "WHERE array_contains(`Sprint`, 'OBIS-20260706-20260717') "
        "LIMIT 3"
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"tools: {len(names)} (has search_by_mql={('search_by_mql' in names)})")

            result = await session.call_tool(
                "search_by_mql",
                arguments={
                    "project_key": "67f5e379dd7f8a00d58f4b0e",
                    "mql": mql,
                },
            )
            if getattr(result, "isError", False):
                print("FAIL: tool returned isError")
                print(result)
                return 1

            texts = []
            for block in result.content or []:
                t = getattr(block, "text", None)
                if t:
                    texts.append(t)
            blob = "\n".join(texts) if texts else ""
            if not blob:
                print("FAIL: empty tool content", result)
                return 1

            # Response may be JSON text; tolerate trailing noise
            start = blob.find("{")
            end = blob.rfind("}")
            if start < 0 or end < start:
                print("FAIL: no JSON object in tool text, len=", len(blob))
                print(blob[:300])
                return 1
            data = json.loads(blob[start : end + 1])
            lst = data.get("list") or []
            count = lst[0].get("count") if lst else None
            page = (data.get("data") or {}).get("1") or []
            page_n = len(page) if isinstance(page, list) else 0
            sid = data.get("session_id")
            print(f"search_by_mql OK: count={count} page_items={page_n} session_id={sid}")
            if page_n:
                # print first story id only
                fields = page[0].get("moql_field_list") or []
                ids = [
                    f.get("value", {}).get("long_value")
                    for f in fields
                    if f.get("name") == "Item Id"
                ]
                print(f"first Item Id: {ids[0] if ids else '?'}")
            print("CONCLUSION: backend MCP client works without LLM / Cursor UI")
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as exc:
        print("FAIL:", type(exc).__name__, exc, file=sys.stderr)
        raise SystemExit(1)

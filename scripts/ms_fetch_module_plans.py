"""按模块名拉取其下全部测试计划（含计划组子计划）的执行数据。"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

BASE = os.environ.get("METERSPHERE_BASE_URL", "https://pixiu.snowballtech.com").rstrip("/")
AK = os.environ["METERSPHERE_ACCESS_KEY"]
SK = os.environ["METERSPHERE_SECRET_KEY"]
ORG = os.environ.get("METERSPHERE_ORGANIZATION", "100001")
PROJECT = os.environ.get("METERSPHERE_PROJECT", "21916479377121280")
MODULE_NAME = os.environ.get("METERSPHERE_MODULE_NAME", "OBIS-20260622-20260703")


def die(msg: str) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(1)


def headers() -> dict[str, str]:
    plain = f"{AK}|{uuid.uuid4()}|{int(time.time() * 1000)}".encode()
    sig = base64.b64encode(
        AES.new(SK.encode(), AES.MODE_CBC, AK.encode()).encrypt(pad(plain, 16))
    ).decode()
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "accessKey": AK,
        "signature": sig,
        "ORGANIZATION": ORG,
        "PROJECT": PROJECT,
    }


def api(method: str, path: str, body: Any = None) -> Any:
    resp = requests.request(method, f"{BASE}{path}", headers=headers(), json=body, timeout=60)
    if "application/json" not in resp.headers.get("Content-Type", ""):
        die(f"{method} {path} non-json HTTP {resp.status_code}")
    data = resp.json()
    if data.get("code") != 100200:
        die(f"{method} {path}: {json.dumps(data, ensure_ascii=False)[:800]}")
    return data.get("data")


def walk_modules(nodes: list[dict], parent: str = "") -> list[dict]:
    out: list[dict] = []
    for n in nodes or []:
        name = n.get("name") or ""
        full = f"{parent}/{name}" if parent else name
        out.append({"id": n.get("id"), "name": name, "fullPath": full, "parentId": n.get("parentId")})
        out.extend(walk_modules(n.get("children") or [], full))
    return out


def find_module(flat: list[dict], target: str) -> dict:
    for m in flat:
        if m["name"] == target:
            return m
    fuzzy = [m for m in flat if target in (m["name"] or "")]
    if fuzzy:
        return fuzzy[0]
    die(f"未找到模块: {target}")


def fetch_plans(module_id: str, group_id: str | None = None) -> list[dict]:
    plans: list[dict] = []
    current = 1
    while True:
        body: dict[str, Any] = {
            "current": current,
            "pageSize": 50,
            "projectId": PROJECT,
            "moduleIds": [module_id],
            "type": "ALL",
            "keyword": "",
            "sort": {},
            "filter": {},
            "combineSearch": {"searchMode": "AND", "conditions": []},
        }
        if group_id:
            body["groupId"] = group_id
        data = api("POST", "/test-plan/page", body)
        rows = data.get("list") or []
        total = int(data.get("total") or 0)
        plans.extend(rows)
        if not rows or len(plans) >= total:
            break
        current += 1
    return plans


def expand_all_plans(module_id: str, top_plans: list[dict]) -> list[dict]:
    """展开 GROUP 下的子计划，返回 [groups + children] 扁平列表。"""
    all_plans: list[dict] = []
    for p in top_plans:
        item = dict(p)
        item["_parentGroupId"] = None
        item["_parentGroupName"] = None
        all_plans.append(item)
        if (p.get("type") or "").upper() == "GROUP":
            # 优先用返回里的 children；否则再按 groupId 查
            children = p.get("children") or []
            if not children and int(p.get("childrenCount") or 0) > 0:
                children = fetch_plans(module_id, group_id=p["id"])
            for c in children:
                child = dict(c)
                child["_parentGroupId"] = p.get("id")
                child["_parentGroupName"] = p.get("name")
                all_plans.append(child)
    return all_plans


def fetch_statistics(plan_ids: list[str]) -> dict[str, dict]:
    if not plan_ids:
        return {}
    result: dict[str, dict] = {}
    chunk = 100
    for i in range(0, len(plan_ids), chunk):
        part = api("POST", "/test-plan/statistics", plan_ids[i : i + chunk]) or []
        for s in part:
            if s.get("id"):
                result[s["id"]] = s
    return result


def compact_plan(p: dict, stats: dict[str, dict]) -> dict[str, Any]:
    sid = p.get("id")
    st = stats.get(sid or "", {})
    return {
        "id": sid,
        "name": p.get("name"),
        "type": p.get("type"),
        "status": st.get("status") or p.get("status"),
        "parentGroupId": p.get("_parentGroupId"),
        "parentGroupName": p.get("_parentGroupName"),
        "moduleId": p.get("moduleId"),
        "passRate": st.get("passRate"),
        "executeRate": st.get("executeRate"),
        "passThreshold": st.get("passThreshold"),
        "pass": st.get("pass"),
        "caseTotal": st.get("caseTotal"),
        "successCount": st.get("successCount"),
        "errorCount": st.get("errorCount"),
        "blockCount": st.get("blockCount"),
        "pendingCount": st.get("pendingCount"),
        "fakeErrorCount": st.get("fakeErrorCount"),
        "functionalCaseCount": st.get("functionalCaseCount"),
        "apiCaseCount": st.get("apiCaseCount"),
        "apiScenarioCount": st.get("apiScenarioCount"),
        "bugCount": st.get("bugCount"),
        "createUserName": p.get("createUserName"),
        "createTime": p.get("createTime"),
    }


def main() -> int:
    print(f"BASE={BASE}")
    print(f"PROJECT={PROJECT}")
    print(f"MODULE_NAME={MODULE_NAME}")

    tree = api("GET", f"/test-plan/module/tree/{PROJECT}") or []
    if not isinstance(tree, list):
        die("module tree 格式异常")
    mod = find_module(walk_modules(tree), MODULE_NAME)
    print(f"[1] module: {mod['fullPath']} ({mod['id']})")

    top_plans = fetch_plans(mod["id"])
    print(f"[2] top plans/groups: {len(top_plans)}")
    all_plans = expand_all_plans(mod["id"], top_plans)
    print(f"[3] expanded plans: {len(all_plans)}")

    ids = [p["id"] for p in all_plans if p.get("id")]
    stats = fetch_statistics(ids)
    print(f"[4] statistics: {len(stats)}")

    rows = [compact_plan(p, stats) for p in all_plans]
    groups = [r for r in rows if (r.get("type") or "").upper() == "GROUP"]
    children = [r for r in rows if (r.get("type") or "").upper() != "GROUP"]

    # 汇总
    def sum_field(items: list[dict], key: str) -> int:
        return sum(int(i.get(key) or 0) for i in items)

    summary = {
        "groupCount": len(groups),
        "planCount": len(children),
        "caseTotal": sum_field(children, "caseTotal"),
        "successCount": sum_field(children, "successCount"),
        "errorCount": sum_field(children, "errorCount"),
        "blockCount": sum_field(children, "blockCount"),
        "pendingCount": sum_field(children, "pendingCount"),
        "bugCount": sum_field(children, "bugCount"),
    }
    if summary["caseTotal"]:
        summary["passRate"] = round(summary["successCount"] * 100.0 / summary["caseTotal"], 2)
    else:
        summary["passRate"] = None

    print("\n=== 执行汇总（子计划合计）===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n=== 计划组 ===")
    for g in groups:
        print(
            f"- {g['name']}: status={g['status']} passRate={g['passRate']}% "
            f"cases={g['caseTotal']} success={g['successCount']} error={g['errorCount']} pending={g['pendingCount']}"
        )
    print("\n=== 子计划 ===")
    for c in children:
        print(
            f"- [{c.get('parentGroupName')}] {c['name']}: status={c['status']} "
            f"passRate={c['passRate']}% cases={c['caseTotal']} "
            f"success={c['successCount']} error={c['errorCount']} pending={c['pendingCount']}"
        )

    out_dir = ROOT / "exports" / "metersphere"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"module_{MODULE_NAME}_execution_{stamp}.json"
    payload = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "module": mod,
        "summary": summary,
        "groups": groups,
        "plans": children,
        "all": rows,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[PASS] {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

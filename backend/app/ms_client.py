from __future__ import annotations

import base64
import time
import uuid
from typing import Any

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .config import settings


class MeterSphereError(RuntimeError):
    pass


class MeterSphereClient:
    def __init__(self) -> None:
        self.base = settings.metersphere_base_url
        self.ak = settings.metersphere_access_key
        self.sk = settings.metersphere_secret_key
        self.org = settings.metersphere_organization
        self.project = settings.metersphere_project
        if not self.ak or not self.sk:
            raise MeterSphereError("缺少 METERSPHERE_ACCESS_KEY / METERSPHERE_SECRET_KEY")

    def _headers(self) -> dict[str, str]:
        plain = f"{self.ak}|{uuid.uuid4()}|{int(time.time() * 1000)}".encode()
        sig = base64.b64encode(
            AES.new(self.sk.encode(), AES.MODE_CBC, self.ak.encode()).encrypt(pad(plain, 16))
        ).decode()
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "accessKey": self.ak,
            "signature": sig,
            "ORGANIZATION": self.org,
            "PROJECT": self.project,
        }

    def api(self, method: str, path: str, body: Any = None) -> Any:
        resp = requests.request(
            method,
            f"{self.base}{path}",
            headers=self._headers(),
            json=body,
            timeout=60,
        )
        if "application/json" not in resp.headers.get("Content-Type", ""):
            raise MeterSphereError(f"{method} {path} non-json HTTP {resp.status_code}")
        data = resp.json()
        if data.get("code") != 100200:
            raise MeterSphereError(f"{method} {path}: {data}")
        return data.get("data")

    @staticmethod
    def walk_modules(nodes: list[dict], parent: str = "") -> list[dict]:
        out: list[dict] = []
        for n in nodes or []:
            name = n.get("name") or ""
            full = f"{parent}/{name}" if parent else name
            out.append(
                {
                    "id": n.get("id"),
                    "name": name,
                    "fullPath": full,
                    "parentId": n.get("parentId"),
                }
            )
            out.extend(MeterSphereClient.walk_modules(n.get("children") or [], full))
        return out

    def list_modules(self) -> list[dict]:
        tree = self.api("GET", f"/test-plan/module/tree/{self.project}") or []
        if not isinstance(tree, list):
            raise MeterSphereError("module tree 格式异常")
        return self.walk_modules(tree)

    def find_module(self, module_id: str | None = None, module_name: str | None = None) -> dict:
        modules = self.list_modules()
        if module_id:
            for m in modules:
                if m["id"] == module_id:
                    return m
        if module_name:
            for m in modules:
                if m["name"] == module_name:
                    return m
            fuzzy = [m for m in modules if module_name in (m["name"] or "")]
            if fuzzy:
                return fuzzy[0]
        raise MeterSphereError(f"未找到模块: id={module_id} name={module_name}")

    def fetch_plans(self, module_id: str, group_id: str | None = None) -> list[dict]:
        plans: list[dict] = []
        current = 1
        while True:
            body: dict[str, Any] = {
                "current": current,
                "pageSize": 50,
                "projectId": self.project,
                "moduleIds": [module_id],
                "type": "ALL",
                "keyword": "",
                "sort": {},
                "filter": {},
                "combineSearch": {"searchMode": "AND", "conditions": []},
            }
            if group_id:
                body["groupId"] = group_id
            data = self.api("POST", "/test-plan/page", body)
            rows = data.get("list") or []
            total = int(data.get("total") or 0)
            plans.extend(rows)
            if not rows or len(plans) >= total:
                break
            current += 1
        return plans

    def expand_all_plans(self, module_id: str, top_plans: list[dict]) -> list[dict]:
        all_plans: list[dict] = []
        for p in top_plans:
            item = dict(p)
            item["_parentGroupId"] = None
            item["_parentGroupName"] = None
            all_plans.append(item)
            if (p.get("type") or "").upper() == "GROUP":
                children = p.get("children") or []
                if not children and int(p.get("childrenCount") or 0) > 0:
                    children = self.fetch_plans(module_id, group_id=p["id"])
                for c in children:
                    child = dict(c)
                    child["_parentGroupId"] = p.get("id")
                    child["_parentGroupName"] = p.get("name")
                    all_plans.append(child)
        return all_plans

    def fetch_statistics(self, plan_ids: list[str]) -> dict[str, dict]:
        if not plan_ids:
            return {}
        result: dict[str, dict] = {}
        for i in range(0, len(plan_ids), 100):
            part = self.api("POST", "/test-plan/statistics", plan_ids[i : i + 100]) or []
            for s in part:
                if s.get("id"):
                    result[s["id"]] = s
        return result

    def fetch_module_execution(self, module_id: str | None = None, module_name: str | None = None) -> dict:
        mod = self.find_module(module_id=module_id, module_name=module_name)
        top_plans = self.fetch_plans(mod["id"])
        all_plans = self.expand_all_plans(mod["id"], top_plans)
        stats = self.fetch_statistics([p["id"] for p in all_plans if p.get("id")])

        children: list[dict] = []
        for p in all_plans:
            if (p.get("type") or "").upper() == "GROUP":
                continue
            st = stats.get(p.get("id") or "", {})
            design = int(st.get("caseTotal") or 0)
            passed = int(st.get("successCount") or 0)
            failed = int(st.get("errorCount") or 0) + int(st.get("fakeErrorCount") or 0)
            blocked = int(st.get("blockCount") or 0) + int(st.get("pendingCount") or 0)
            children.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "parentGroupName": p.get("_parentGroupName"),
                    "design": design,
                    "passed": passed,
                    "failed": failed,
                    "blocked": blocked,
                    "status": st.get("status") or p.get("status"),
                }
            )

        children.sort(key=lambda x: (x.get("parentGroupName") or "", x.get("name") or ""))
        case_total = sum(r["design"] for r in children)
        success_total = sum(r["passed"] for r in children)
        summary = {
            "planCount": len(children),
            "caseTotal": case_total,
            "successCount": success_total,
            "errorCount": sum(r["failed"] for r in children),
            "blockCount": sum(r["blocked"] for r in children),
            "passRate": round(success_total * 100.0 / case_total, 2) if case_total else None,
        }
        return {"module": mod, "summary": summary, "plans": children}

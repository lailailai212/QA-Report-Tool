"""MeterSphere 测试执行数据自动拉取（AccessKey 签名认证，适合脚本/CI）。

一次性配置：
  1. 登录 https://pixiu.snowballtech.com
  2. 右上角头像 → 个人中心 → API Keys → 生成
  3. 复制 Access Key / Secret Key 到环境变量或 .env

用法（PowerShell）:
  $env:METERSPHERE_BASE_URL="https://pixiu.snowballtech.com"
  $env:METERSPHERE_ACCESS_KEY="你的AK"
  $env:METERSPHERE_SECRET_KEY="你的SK"
  $env:METERSPHERE_ORGANIZATION="100001"
  $env:METERSPHERE_PROJECT="21916479377121280"
  python scripts/ms_fetch_reports.py

输出：
  exports/metersphere/reports_<日期>.json
"""
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

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
except ImportError:
    print("[FAIL] 缺少依赖，请先执行: pip install pycryptodome requests")
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(ENV_FILE)

BASE = os.environ.get("METERSPHERE_BASE_URL", "https://pixiu.snowballtech.com").rstrip("/")
ACCESS_KEY = os.environ.get("METERSPHERE_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("METERSPHERE_SECRET_KEY", "")
ORG = os.environ.get("METERSPHERE_ORGANIZATION", "100001")
PROJECT = os.environ.get("METERSPHERE_PROJECT", "21916479377121280")
PAGE_SIZE = int(os.environ.get("METERSPHERE_PAGE_SIZE", "20"))
MAX_PAGES = int(os.environ.get("METERSPHERE_MAX_PAGES", "5"))
DETAIL_LIMIT = int(os.environ.get("METERSPHERE_DETAIL_LIMIT", "5"))


def die(msg: str, code: int = 1) -> None:
    print(f"[FAIL] {msg}")
    sys.exit(code)


def aes_signature(access_key: str, secret_key: str) -> str:
    """MS 官方签名：AES/CBC/PKCS5Padding，明文 = AK|UUID|timestamp。"""
    if len(access_key) != 16 or len(secret_key) != 16:
        die("AccessKey / SecretKey 长度必须为 16 位（MeterSphere 生成的标准长度）")
    plain = f"{access_key}|{uuid.uuid4()}|{int(time.time() * 1000)}".encode("utf-8")
    cipher = AES.new(secret_key.encode("utf-8"), AES.MODE_CBC, access_key.encode("utf-8"))
    encrypted = cipher.encrypt(pad(plain, AES.block_size))
    return base64.b64encode(encrypted).decode("utf-8")


def auth_headers() -> dict[str, str]:
    if not ACCESS_KEY or not SECRET_KEY:
        die(
            "缺少 METERSPHERE_ACCESS_KEY / METERSPHERE_SECRET_KEY。\n"
            "请在 MeterSphere：个人中心 → API Keys 生成后写入 .env 或环境变量。"
        )
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "accessKey": ACCESS_KEY,
        "signature": aes_signature(ACCESS_KEY, SECRET_KEY),
        "ORGANIZATION": ORG,
        "PROJECT": PROJECT,
    }


def api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    url = f"{BASE}{path}"
    # 每次请求重新签名，避免过期
    resp = requests.request(
        method,
        url,
        headers=auth_headers(),
        json=body,
        timeout=60,
    )
    ct = resp.headers.get("Content-Type", "")
    if "application/json" not in ct:
        die(f"{method} {path} 返回非 JSON（HTTP {resp.status_code}），请检查 AK/SK 或网关路由")
    data = resp.json()
    code = data.get("code")
    if code not in (None, 100200, 200, 0) and data.get("success") is not True:
        # MS3 成功码通常是 100200
        if code != 100200:
            die(f"{method} {path} 业务失败: {json.dumps(data, ensure_ascii=False)[:800]}")
    return data.get("data", data)


def fetch_report_page(current: int) -> dict[str, Any]:
    return api(
        "POST",
        "/test-plan/report/page",
        {
            "current": current,
            "pageSize": PAGE_SIZE,
            "projectId": PROJECT,
            "sort": {},
        },
    )


def fetch_report_detail(report_id: str) -> dict[str, Any]:
    return api("GET", f"/test-plan/report/get/{report_id}")


def fetch_functional_cases(report_id: str) -> dict[str, Any]:
    return api(
        "POST",
        "/test-plan/report/detail/functional/case/page",
        {"reportId": report_id, "current": 1, "pageSize": 5},
    )


def summarize_report(detail: dict[str, Any]) -> dict[str, Any]:
    execute = detail.get("executeCount") or {}
    return {
        "id": detail.get("id"),
        "name": detail.get("name"),
        "testPlanName": detail.get("testPlanName"),
        "resultStatus": detail.get("resultStatus"),
        "passRate": detail.get("passRate"),
        "executeRate": detail.get("executeRate"),
        "caseTotal": detail.get("caseTotal"),
        "functionalTotal": detail.get("functionalTotal"),
        "apiCaseTotal": detail.get("apiCaseTotal"),
        "apiScenarioTotal": detail.get("apiScenarioTotal"),
        "bugCount": detail.get("bugCount"),
        "success": execute.get("success"),
        "error": execute.get("error"),
        "block": execute.get("block"),
        "pending": execute.get("pending"),
        "createTime": detail.get("createTime"),
        "startTime": detail.get("startTime"),
        "endTime": detail.get("endTime"),
    }


def main() -> int:
    print(f"BASE={BASE}")
    print(f"ORG={ORG} PROJECT={PROJECT}")
    print("[1] 使用 AccessKey 签名认证拉取数据...")

    # 连通性：版本接口通常匿名可读；失败也不阻断
    try:
        ver = requests.get(f"{BASE}/system/version/current", timeout=20).json()
        print(f"[2] version: {ver.get('data')}")
    except Exception as exc:
        print(f"[2] version 跳过: {exc}")

    reports: list[dict[str, Any]] = []
    total = None
    for page in range(1, MAX_PAGES + 1):
        data = fetch_report_page(page)
        rows = data.get("list") or []
        total = data.get("total", total)
        print(f"[3] report/page current={page} got={len(rows)} total={total}")
        reports.extend(rows)
        if not rows or (total is not None and len(reports) >= int(total)):
            break

    if not reports:
        die("未拉到任何测试报告，请确认 PROJECT 是否正确、账号是否有报告权限")

    details: list[dict[str, Any]] = []
    for row in reports[:DETAIL_LIMIT]:
        rid = row["id"]
        detail = fetch_report_detail(rid)
        summary = summarize_report(detail if isinstance(detail, dict) else {})
        try:
            cases = fetch_functional_cases(rid)
            summary["functionalCaseSampleTotal"] = cases.get("total")
            summary["functionalCaseSample"] = [
                {
                    "name": c.get("name") or c.get("caseName"),
                    "result": c.get("executeResult") or c.get("status"),
                }
                for c in (cases.get("list") or [])[:5]
            ]
        except SystemExit:
            raise
        except Exception as exc:
            summary["functionalCaseError"] = str(exc)
        details.append(summary)
        print(
            f"    - {summary.get('name')} | {summary.get('resultStatus')} | "
            f"passRate={summary.get('passRate')}% | cases={summary.get('caseTotal')}"
        )

    out_dir = ROOT / "exports" / "metersphere"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"reports_{stamp}.json"
    payload = {
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "baseUrl": BASE,
        "organizationId": ORG,
        "projectId": PROJECT,
        "reportTotal": total,
        "reportListCount": len(reports),
        "reportList": [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "planName": r.get("planName"),
                "execStatus": r.get("execStatus"),
                "resultStatus": r.get("resultStatus"),
                "passRate": r.get("passRate"),
                "createTime": r.get("createTime"),
                "createUserName": r.get("createUserName"),
            }
            for r in reports
        ],
        "reportDetails": details,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[PASS] 已生成数据文件: {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.RequestException as exc:
        die(f"HTTP 错误: {exc}")

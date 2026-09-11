# -*- coding: utf-8 -*-
"""Fetch Genbu component current versions via Beast SSO token exchange."""
from __future__ import annotations

import threading
import time
from typing import Any

import requests

from .config import settings

# Feishu Bug Environment option id / label -> Genbu env
FEISHU_ENV_TO_GENBU = {
    "hkrnplf2t": "SIT",
    "xu3bhhm0q": "UAT",
    "rsw18358z": "PRE",
    "_rme3lbye": "PRD",
    "SIT": "SIT",
    "UAT": "UAT",
    "PRE": "PRE",
    "PRD": "PRD",
}


class GenbuError(RuntimeError):
    pass


class GenbuClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._genbu_token: str | None = None
        self._genbu_token_exp: float = 0.0  # epoch seconds; 0 = unknown

    def configured(self) -> bool:
        return bool(
            (settings.beast_username or "").strip()
            and (settings.beast_password or "").strip()
        )

    def resolve_env(self, env: str) -> str:
        key = (env or "").strip()
        if not key:
            raise GenbuError("请指定环境（SIT / UAT / PRE / PRD）")
        mapped = FEISHU_ENV_TO_GENBU.get(key) or FEISHU_ENV_TO_GENBU.get(key.upper())
        if not mapped:
            raise GenbuError(f"不支持的环境：{env}")
        return mapped

    def _beast_login(self) -> str:
        user = (settings.beast_username or "").strip()
        pwd = (settings.beast_password or "").strip()
        if not user or not pwd:
            raise GenbuError("未配置 BEAST_USERNAME / BEAST_PASSWORD")
        url = f"{settings.beast_base_url.rstrip('/')}/beast/api/login"
        try:
            resp = requests.post(
                url,
                json={"username": user, "password": pwd},
                headers={"Content-Type": "application/json", "Accept": "*/*"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise GenbuError(f"Beast 登录失败: {exc}") from exc
        if resp.status_code >= 400:
            raise GenbuError(f"Beast 登录 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise GenbuError(f"Beast 登录返回非 JSON: {resp.text[:200]!r}") from exc
        code = str(data.get("code") or "")
        if code not in ("000000", "0", "200", "20000"):
            raise GenbuError(
                f"Beast 登录失败: {data.get('message') or data.get('code') or data}"
            )
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        token = str((payload or {}).get("token") or "").strip()
        if not token:
            raise GenbuError("Beast 登录成功但未返回 token")
        return token

    def _exchange_genbu_token(self, beast_token: str) -> str:
        url = f"{settings.genbu_base_url.rstrip('/')}/genbu/admin/api/dragonUser"
        try:
            resp = requests.get(
                url,
                params={"token": beast_token},
                headers={"Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise GenbuError(f"Genbu 换票失败: {exc}") from exc
        if resp.status_code >= 400:
            raise GenbuError(f"Genbu 换票 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise GenbuError(f"Genbu 换票返回非 JSON: {resp.text[:200]!r}") from exc
        if int(data.get("code") or 0) != 20000:
            raise GenbuError(
                f"Genbu 换票失败: {data.get('message') or data.get('code') or data}"
            )
        token = str(data.get("token") or "").strip()
        if not token:
            raise GenbuError("Genbu 换票成功但未返回 token")
        return token

    def _token_apparently_expired(self) -> bool:
        if not self._genbu_token:
            return True
        if self._genbu_token_exp <= 0:
            return False
        # refresh 2 minutes early
        return time.time() >= (self._genbu_token_exp - 120)

    @staticmethod
    def _parse_jwt_exp(token: str) -> float:
        try:
            import base64
            import json

            parts = token.split(".")
            if len(parts) < 2:
                return 0.0
            pad = "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
            exp = payload.get("exp")
            return float(exp) if exp is not None else 0.0
        except Exception:
            return 0.0

    def get_genbu_token(self, *, force: bool = False) -> str:
        with self._lock:
            if not force and not self._token_apparently_expired():
                return self._genbu_token  # type: ignore[return-value]
            beast = self._beast_login()
            token = self._exchange_genbu_token(beast)
            self._genbu_token = token
            self._genbu_token_exp = self._parse_jwt_exp(token)
            return token

    def _version_page(
        self,
        token: str,
        *,
        env: str,
        current: int,
        size: int,
    ) -> dict[str, Any]:
        url = f"{settings.genbu_base_url.rstrip('/')}/genbu/version/summary/page"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": settings.genbu_base_url.rstrip("/"),
            "Referer": settings.genbu_base_url.rstrip("/") + "/",
            "app-id": settings.genbu_app_id,
            "system-code": settings.genbu_system_code,
            "x-token": token,
        }
        body = {
            "current": current,
            "size": size,
            "product_line": settings.genbu_product_line,
            "env": env,
            "app_names": [],
            "start_time": None,
            "end_time": None,
        }
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=45)
        except requests.RequestException as exc:
            raise GenbuError(f"Genbu 版本查询失败: {exc}") from exc
        if resp.status_code == 401:
            raise GenbuError("UNAUTHORIZED")
        if resp.status_code >= 400:
            raise GenbuError(f"Genbu 版本查询 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise GenbuError(f"Genbu 版本查询返回非 JSON: {resp.text[:200]!r}") from exc
        code = data.get("code")
        if int(code or 0) == 50000 and "认证" in str(data.get("message") or ""):
            raise GenbuError("UNAUTHORIZED")
        if int(code or 0) != 20000:
            raise GenbuError(
                f"Genbu 版本查询失败: {data.get('message') or data.get('code') or data}"
            )
        response = data.get("response")
        if not isinstance(response, dict):
            raise GenbuError("Genbu 版本查询 response 格式异常")
        return response

    def fetch_versions(self, env: str) -> dict[str, Any]:
        genbu_env = self.resolve_env(env)
        if not self.configured():
            raise GenbuError("未配置 BEAST_USERNAME / BEAST_PASSWORD，无法拉取 Genbu 版本")

        token = self.get_genbu_token()
        try:
            page = self._version_page(token, env=genbu_env, current=1, size=50)
        except GenbuError as exc:
            if str(exc) != "UNAUTHORIZED":
                raise
            token = self.get_genbu_token(force=True)
            page = self._version_page(token, env=genbu_env, current=1, size=50)

        total = int(page.get("total") or 0)
        rows: list[dict[str, Any]] = list(page.get("data") or [])
        size = int(page.get("size") or 50) or 50
        current = int(page.get("current") or 1)
        # paginate
        while len(rows) < total and current * size < total + size:
            current += 1
            if current > 40:
                raise GenbuError(f"Genbu 分页超过安全上限（已拉 {len(rows)}/{total}）")
            more = self._version_page(token, env=genbu_env, current=current, size=size)
            chunk = list(more.get("data") or [])
            if not chunk:
                break
            rows.extend(chunk)

        # dedupe by (app_name, namespace), keep latest deploy_time
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("app_name") or "").strip()
            ns = str(raw.get("namespace") or "").strip()
            ver = str(raw.get("current_version") or "").strip()
            if not name or not ver:
                continue
            key = (name, ns)
            prev = best.get(key)
            if not prev:
                best[key] = raw
                continue
            if str(raw.get("deploy_time") or "") >= str(prev.get("deploy_time") or ""):
                best[key] = raw

        items = sorted(
            best.values(),
            key=lambda x: (
                str(x.get("app_name") or ""),
                str(x.get("namespace") or ""),
            ),
        )
        lines = [_format_component_line(x, default_env=genbu_env) for x in items]
        text = "\n".join(lines)
        return {
            "env": genbu_env,
            "productLine": settings.genbu_product_line,
            "count": len(items),
            "text": text,
            "items": [
                {
                    "title": str(x.get("app_name_cn") or "").strip()
                    or str(x.get("app_name") or ""),
                    "appName": str(x.get("app_name") or ""),
                    "appNameCn": str(x.get("app_name_cn") or ""),
                    "env": str(x.get("env") or genbu_env),
                    "namespace": str(x.get("namespace") or ""),
                    "currentVersion": str(x.get("current_version") or ""),
                    "deployTime": str(x.get("deploy_time") or ""),
                }
                for x in items
            ],
        }


def _format_component_line(row: dict[str, Any], *, default_env: str) -> str:
    title = str(row.get("app_name_cn") or "").strip() or str(row.get("app_name") or "").strip()
    app_name = str(row.get("app_name") or "").strip()
    env = str(row.get("env") or default_env).strip()
    namespace = str(row.get("namespace") or "").strip() or "-"
    version = str(row.get("current_version") or "").strip()
    return (
        f"Title: {title} | 服务名称: {app_name} | 发布环境: {env} | "
        f"命名空间: {namespace} | 版本: {version}"
    )


genbu_client = GenbuClient()

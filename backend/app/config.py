from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")


class Settings:
    metersphere_base_url: str = os.environ.get(
        "METERSPHERE_BASE_URL", "https://pixiu.snowballtech.com"
    ).rstrip("/")
    metersphere_access_key: str = os.environ.get("METERSPHERE_ACCESS_KEY", "")
    metersphere_secret_key: str = os.environ.get("METERSPHERE_SECRET_KEY", "")
    metersphere_organization: str = os.environ.get("METERSPHERE_ORGANIZATION", "100001")
    metersphere_project: str = os.environ.get("METERSPHERE_PROJECT", "21916479377121280")

    smtp_host: str = os.environ.get("SMTP_HOST", "smtp.feishu.cn")
    smtp_port: int = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_password: str = os.environ.get("SMTP_PASSWORD", "")
    smtp_from: str = os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", "")
    smtp_use_ssl: bool = os.environ.get("SMTP_USE_SSL", "true").lower() in {
        "1",
        "true",
        "yes",
    }

    timezone: str = os.environ.get("TZ", "Asia/Shanghai")
    db_path: Path = ROOT / "backend" / "data" / "report_tool.db"

    # Feishu Project MCP (snapshot refresh; no LLM)
    mcp_user_token: str = os.environ.get("MCP_USER_TOKEN", "")
    feishu_mcp_domain: str = os.environ.get(
        "FEISHU_MCP_DOMAIN", "https://project.feishu.cn"
    ).rstrip("/")
    feishu_project_key: str = os.environ.get(
        "FEISHU_PROJECT_KEY", "67f5e379dd7f8a00d58f4b0e"
    )
    feishu_simple_name: str = os.environ.get("FEISHU_SIMPLE_NAME", "obis")
    feishu_snapshot_weekdays: str = os.environ.get(
        "FEISHU_SNAPSHOT_WEEKDAYS", "1,2,3,4,5"
    )
    feishu_snapshot_time: str = os.environ.get("FEISHU_SNAPSHOT_TIME", "20:20")
    feishu_snapshot_sprint: str = os.environ.get("FEISHU_SNAPSHOT_SPRINT", "").strip()

    # OpenAI-compatible LLM (Bug Description)；默认 DeepSeek
    ai_api_key: str = (
        os.environ.get("DEEPSEEK_API_KEY", "").strip()
        or os.environ.get("AI_API_KEY", "").strip()
    )
    ai_base_url: str = os.environ.get(
        "AI_BASE_URL",
        "https://api.deepseek.com",
    ).rstrip("/")
    ai_model: str = (
        os.environ.get("AI_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    )

    # Genbu Component Versions（Beast 登录 → dragonUser 换票 → 查版本）
    beast_base_url: str = os.environ.get(
        "BEAST_BASE_URL", "https://beast.snowballtech.com"
    ).rstrip("/")
    beast_username: str = os.environ.get("BEAST_USERNAME", "").strip()
    beast_password: str = os.environ.get("BEAST_PASSWORD", "").strip()
    genbu_base_url: str = os.environ.get(
        "GENBU_BASE_URL", "https://genbu.snowballtech.com"
    ).rstrip("/")
    genbu_product_line: str = (
        os.environ.get("GENBU_PRODUCT_LINE", "IOT").strip() or "IOT"
    )
    genbu_app_id: str = os.environ.get("GENBU_APP_ID", "778").strip() or "778"
    genbu_system_code: str = (
        os.environ.get("GENBU_SYSTEM_CODE", "IOT").strip() or "IOT"
    )

    # Sprint 总结报告：默认飞书导出 xlsx 目录（5 个文件）
    sprint_summary_xlsx_dir: str = os.environ.get("SPRINT_SUMMARY_XLSX_DIR", "").strip()


settings = Settings()

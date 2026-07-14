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


settings = Settings()

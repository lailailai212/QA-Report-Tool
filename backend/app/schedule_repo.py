from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .config import settings


@dataclass
class Schedule:
    id: str
    name: str
    module_id: str
    module_name: str
    to_emails: list[str]
    cc_emails: list[str]
    subject_template: str
    weekdays: list[int]
    time: str
    enabled: bool
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScheduleRepo:
    def __init__(self, db_path=None) -> None:
        self.db_path = db_path or settings.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    module_name TEXT NOT NULL,
                    to_emails TEXT NOT NULL,
                    cc_emails TEXT NOT NULL,
                    subject_template TEXT NOT NULL,
                    weekdays TEXT NOT NULL,
                    time TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    last_run_at TEXT,
                    last_status TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> Schedule:
        return Schedule(
            id=row["id"],
            name=row["name"],
            module_id=row["module_id"],
            module_name=row["module_name"],
            to_emails=json.loads(row["to_emails"]),
            cc_emails=json.loads(row["cc_emails"]),
            subject_template=row["subject_template"],
            weekdays=json.loads(row["weekdays"]),
            time=row["time"],
            enabled=bool(row["enabled"]),
            last_run_at=row["last_run_at"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_all(self) -> list[Schedule]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM schedules ORDER BY created_at DESC").fetchall()
        return [self._row_to_schedule(r) for r in rows]

    def get(self, schedule_id: str) -> Schedule | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return self._row_to_schedule(row) if row else None

    def create(self, payload: dict[str, Any]) -> Schedule:
        now = datetime.now().isoformat(timespec="seconds")
        item = Schedule(
            id=str(uuid.uuid4()),
            name=payload["name"],
            module_id=payload["module_id"],
            module_name=payload["module_name"],
            to_emails=payload.get("to_emails") or [],
            cc_emails=payload.get("cc_emails") or [],
            subject_template=payload.get("subject_template")
            or "Sprint_Daily_Report_{date} 【{module}】",
            weekdays=payload.get("weekdays") or [1, 2, 3, 4, 5],
            time=payload["time"],
            enabled=bool(payload.get("enabled", True)),
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedules (
                    id, name, module_id, module_name, to_emails, cc_emails,
                    subject_template, weekdays, time, enabled,
                    last_run_at, last_status, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.name,
                    item.module_id,
                    item.module_name,
                    json.dumps(item.to_emails, ensure_ascii=False),
                    json.dumps(item.cc_emails, ensure_ascii=False),
                    item.subject_template,
                    json.dumps(item.weekdays),
                    item.time,
                    1 if item.enabled else 0,
                    None,
                    None,
                    None,
                    item.created_at,
                    item.updated_at,
                ),
            )
            conn.commit()
        return item

    def update(self, schedule_id: str, payload: dict[str, Any]) -> Schedule:
        current = self.get(schedule_id)
        if not current:
            raise KeyError(schedule_id)
        data = current.to_dict()
        data.update({k: v for k, v in payload.items() if v is not None and k != "id"})
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE schedules SET
                    name=?, module_id=?, module_name=?, to_emails=?, cc_emails=?,
                    subject_template=?, weekdays=?, time=?, enabled=?,
                    last_run_at=?, last_status=?, last_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    data["name"],
                    data["module_id"],
                    data["module_name"],
                    json.dumps(data["to_emails"], ensure_ascii=False),
                    json.dumps(data["cc_emails"], ensure_ascii=False),
                    data["subject_template"],
                    json.dumps(data["weekdays"]),
                    data["time"],
                    1 if data["enabled"] else 0,
                    data.get("last_run_at"),
                    data.get("last_status"),
                    data.get("last_error"),
                    data["updated_at"],
                    schedule_id,
                ),
            )
            conn.commit()
        return self.get(schedule_id)  # type: ignore[return-value]

    def delete(self, schedule_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
            conn.commit()

    def mark_run(self, schedule_id: str, status: str, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE schedules SET last_run_at=?, last_status=?, last_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    status,
                    error,
                    datetime.now().isoformat(timespec="seconds"),
                    schedule_id,
                ),
            )
            conn.commit()

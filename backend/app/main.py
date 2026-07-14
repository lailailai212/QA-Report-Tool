from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .email_sender import EmailSender
from .ms_client import MeterSphereClient, MeterSphereError
from .report_service import build_report, render_html
from .schedule_repo import ScheduleRepo
from .scheduler import schedule_manager

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    schedule_manager.start()
    yield
    schedule_manager.shutdown()


app = FastAPI(title="QA Report Tool", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

repo = ScheduleRepo()
mailer = EmailSender()


class PreviewRequest(BaseModel):
    module_id: str | None = None
    module_name: str | None = None
    test_env: str = ""
    risk_block: str = ""
    mode: str = "manual"


class SendRequest(BaseModel):
    module_id: str | None = None
    module_name: str | None = None
    test_env: str = ""
    risk_block: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str | None = None


class SchedulePayload(BaseModel):
    name: str
    module_id: str
    module_name: str
    to_emails: list[str]
    cc_emails: list[str] = Field(default_factory=list)
    subject_template: str = "Sprint_Daily_Report_{date} 【{module}】"
    weekdays: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    time: str
    enabled: bool = True


class ScheduleUpdatePayload(BaseModel):
    name: str | None = None
    module_id: str | None = None
    module_name: str | None = None
    to_emails: list[str] | None = None
    cc_emails: list[str] | None = None
    subject_template: str | None = None
    weekdays: list[int] | None = None
    time: str | None = None
    enabled: bool | None = None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/modules")
def list_modules() -> list[dict[str, Any]]:
    try:
        return MeterSphereClient().list_modules()
    except MeterSphereError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/reports/preview")
def preview_report(body: PreviewRequest) -> dict[str, Any]:
    mode = "scheduled" if body.mode == "scheduled" else "manual"
    try:
        report = build_report(
            mode=mode,
            module_id=body.module_id,
            module_name=body.module_name,
            test_env=body.test_env,
            risk_block=body.risk_block,
        )
        html = render_html(report)
        return {"report": report, "html": html}
    except MeterSphereError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/reports/send")
def send_report(body: SendRequest) -> dict[str, Any]:
    try:
        report = build_report(
            mode="manual",
            module_id=body.module_id,
            module_name=body.module_name,
            test_env=body.test_env,
            risk_block=body.risk_block,
        )
        html = render_html(report)
        subject = body.subject or (
            f"Sprint_Daily_Report_{datetime.now().strftime('%m/%d')} 【{report['moduleName']}】"
        )
        mailer.send_html(subject=subject, html=html, to_emails=body.to, cc_emails=body.cc)
        return {"ok": True, "subject": subject, "rows": len(report["rows"])}
    except MeterSphereError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"发送失败: {exc}") from exc


@app.get("/api/schedules")
def list_schedules() -> list[dict[str, Any]]:
    items = []
    for s in repo.list_all():
        d = s.to_dict()
        d["next_run_time"] = schedule_manager.next_run_time(s.id)
        items.append(d)
    return items


@app.post("/api/schedules")
def create_schedule(body: SchedulePayload) -> dict[str, Any]:
    if not body.to_emails:
        raise HTTPException(status_code=400, detail="to_emails 不能为空")
    item = repo.create(body.model_dump())
    schedule_manager.upsert_job(item)
    d = item.to_dict()
    d["next_run_time"] = schedule_manager.next_run_time(item.id)
    return d


@app.put("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: str, body: ScheduleUpdatePayload) -> dict[str, Any]:
    try:
        item = repo.update(schedule_id, body.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="schedule not found") from exc
    schedule_manager.upsert_job(item)
    d = item.to_dict()
    d["next_run_time"] = schedule_manager.next_run_time(item.id)
    return d


@app.delete("/api/schedules/{schedule_id}")
def delete_schedule(schedule_id: str) -> dict[str, bool]:
    repo.delete(schedule_id)
    schedule_manager.remove_job(schedule_id)
    return {"ok": True}


@app.post("/api/schedules/{schedule_id}/run-now")
def run_schedule_now(schedule_id: str) -> dict[str, Any]:
    try:
        return schedule_manager.run_schedule(schedule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="schedule not found") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

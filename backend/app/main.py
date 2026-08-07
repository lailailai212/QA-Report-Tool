from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
import json

from .bug_meta import bug_meta
from .bug_service import create_bug_from_template
from .bug_template_store import (
    copy_template,
    create_template,
    delete_template,
    get_template,
    list_templates,
    update_template,
)
from .email_sender import EmailSender
from .feishu_mcp import (
    FeishuMcpError,
    format_mcp_exception,
    list_sprint_names,
    list_versions,
    root_exception,
    search_users,
)
from .feishu_refresh import (
    FeishuRefreshBusy,
    FeishuRefreshConfigError,
    FeishuValidateError,
    read_last_status,
    refresh_sprint,
)
from .ms_client import MeterSphereClient, MeterSphereError
from .override_locks import (
    acquire_lock,
    get_locks,
    heartbeat_lock,
    release_lock,
)
from .override_store import (
    OVERALL_RESULT_OPTIONS,
    OverrideConflictError,
    load_override,
    save_override,
)
from .report_service import (
    build_completion_report,
    build_report,
    render_completion_html,
    render_html,
)
from .schedule_repo import ScheduleRepo
from .scheduler import schedule_manager
from .timeutil import now_beijing_mmdd

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


class CompletionPreviewRequest(BaseModel):
    module_id: str | None = None
    module_name: str | None = None
    test_env: str = ""
    risk_block: str = ""


class CompletionSendRequest(BaseModel):
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


class OverrideStoryFields(BaseModel):
    ready: str | None = None
    readyDate: str | None = None
    comment: str | None = None


class OverrideReopenRow(BaseModel):
    priority: str = ""
    status: str = ""
    summary: str
    url: str = ""
    reopenTimes: int = 0


class OverrideRev(BaseModel):
    meta: int | None = None
    stories: int | None = None
    reopen: int | None = None
    completion: int | None = None


class CompletionExitCriteria(BaseModel):
    execPassMin: int = 95
    p0p1OpenMax: int = 0


class CompletionSignOffRow(BaseModel):
    role: str
    name: str = ""
    result: str = ""
    date: str = ""


class CompletionOverride(BaseModel):
    overallResult: str = ""
    testOwner: str = ""
    testWindowStart: str = ""
    testWindowEnd: str = ""
    exitCriteria: CompletionExitCriteria = Field(
        default_factory=CompletionExitCriteria
    )
    summaryOneLiner: str = ""
    deferredItems: str = ""
    recommendations: str = ""
    signOff: list[CompletionSignOffRow] = Field(default_factory=list)
    openBugNotes: dict[str, str] = Field(default_factory=dict)


class OverridePayload(BaseModel):
    testEnv: str | None = None
    riskBlock: str | None = None
    stories: dict[str, OverrideStoryFields] | None = None
    # omit = leave unchanged; null = revert to feishu; list = manual replace
    reopenRows: list[OverrideReopenRow] | None = None
    clearReopenManual: bool = False
    completion: CompletionOverride | None = None
    expectedRev: OverrideRev | None = None


class OverrideLockRequest(BaseModel):
    section: str
    editor: str = ""
    token: str | None = None


class FeishuRefreshRequest(BaseModel):
    sprint: str


class BugPerson(BaseModel):
    userKey: str
    name: str = ""


class BugTemplateFields(BaseModel):
    summary: str = ""
    description: str = ""
    sprintIds: list[int] = Field(default_factory=list)
    issueTypeIds: list[str] = Field(default_factory=list)
    executionMethodId: str = ""
    labelIds: list[str] = Field(default_factory=list)
    issueStageId: str = ""
    bugEnvironmentId: str = ""
    componentVersions: str = ""
    versionFoundIds: list[int] = Field(default_factory=list)
    severityId: str = ""
    priorityId: str = ""


class BugTemplateRoles(BaseModel):
    devOwner: list[BugPerson] = Field(default_factory=list)
    qcOwner: list[BugPerson] = Field(default_factory=list)
    reporter: list[BugPerson] = Field(default_factory=list)


class BugTemplateLabels(BaseModel):
    sprint: str = ""
    versionFound: list[str] = Field(default_factory=list)


class BugTemplatePayload(BaseModel):
    name: str
    projectKey: str | None = None
    simpleName: str | None = None
    feishuTemplateId: str = "4362903"
    fields: BugTemplateFields = Field(default_factory=BugTemplateFields)
    roles: BugTemplateRoles = Field(default_factory=BugTemplateRoles)
    labels: BugTemplateLabels = Field(default_factory=BugTemplateLabels)


class CreateBugImage(BaseModel):
    fileName: str = "paste.png"
    mimeType: str = "image/png"
    contentBase64: str


class CreateBugRequest(BaseModel):
    templateId: str
    summary: str
    description: str = ""
    severityId: str = ""
    priorityId: str = ""
    componentVersions: str = ""
    images: list[CreateBugImage] = Field(default_factory=list)


def _override_with_locks(sprint: str) -> dict[str, Any]:
    data = load_override(sprint)
    data["locks"] = get_locks(sprint)
    return data


def _conflict_http(exc: OverrideConflictError) -> HTTPException:
    labels = {"meta": "ENV/Risk", "stories": "Ready", "reopen": "Reopen"}
    label = labels.get(exc.section, exc.section)
    current = dict(exc.current)
    current["locks"] = get_locks(current.get("sprint") or "")
    return HTTPException(
        status_code=409,
        detail={
            "code": "override_conflict",
            "section": exc.section,
            "message": (
                f"「{label}」已被他人更新（你打开时 rev={exc.expected}，"
                f"当前 rev={exc.actual}）。请重新加载后再保存。"
            ),
            "expected": exc.expected,
            "actual": exc.actual,
            "current": current,
        },
    )


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (STATIC_DIR / "home.html").read_text(encoding="utf-8")


@app.get("/report", response_class=HTMLResponse)
def report() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/completion", response_class=HTMLResponse)
def completion_page() -> str:
    return (STATIC_DIR / "completion.html").read_text(encoding="utf-8")


@app.get("/help", response_class=HTMLResponse)
def help_doc() -> str:
    return (STATIC_DIR / "help.html").read_text(encoding="utf-8")


@app.get("/bug", response_class=HTMLResponse)
def bug_page() -> str:
    return (STATIC_DIR / "bug.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/bug/meta")
def api_bug_meta() -> dict[str, Any]:
    return bug_meta()


@app.get("/api/bug-templates")
def api_list_bug_templates() -> dict[str, Any]:
    return {"templates": list_templates()}


@app.get("/api/bug-templates/{template_id}")
def api_get_bug_template(template_id: str) -> dict[str, Any]:
    item = get_template(template_id)
    if not item:
        raise HTTPException(status_code=404, detail="模板不存在")
    return item


@app.post("/api/bug-templates")
def api_create_bug_template(body: BugTemplatePayload) -> dict[str, Any]:
    try:
        return create_template(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/bug-templates/{template_id}")
def api_update_bug_template(template_id: str, body: BugTemplatePayload) -> dict[str, Any]:
    try:
        return update_template(template_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模板不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/bug-templates/{template_id}")
def api_delete_bug_template(template_id: str) -> dict[str, str]:
    if not delete_template(template_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"status": "ok"}


@app.post("/api/bug-templates/{template_id}/copy")
def api_copy_bug_template(template_id: str) -> dict[str, Any]:
    try:
        return copy_template(template_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模板不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/bug/users")
def api_bug_users(q: str = Query("", min_length=0)) -> dict[str, Any]:
    query = (q or "").strip()
    if not query:
        return {"users": []}
    try:
        users = search_users([query])
        return {"users": users}
    except FeishuMcpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/bug/sprints")
def api_bug_sprints() -> dict[str, Any]:
    try:
        return {"sprints": list_sprint_names()}
    except FeishuMcpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/bug/versions")
def api_bug_versions(q: str = Query("")) -> dict[str, Any]:
    try:
        return {"versions": list_versions(q)}
    except FeishuMcpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/bugs")
async def api_create_bug(request: Request) -> dict[str, Any]:
    """
    Create Bug.

    - application/json: CreateBugRequest (optional base64 images; fine for no/small images)
    - multipart/form-data: field `meta` (JSON) + repeated file field `images`
    """
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "multipart/form-data" in ctype:
            form = await request.form()
            meta_raw = form.get("meta")
            if meta_raw is None:
                raise HTTPException(status_code=400, detail="缺少 meta 字段")
            try:
                meta = json.loads(str(meta_raw))
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail=f"meta JSON 无效: {exc}") from exc
            if not isinstance(meta, dict):
                raise HTTPException(status_code=400, detail="meta 必须是 JSON 对象")

            images: list[dict[str, Any]] = []
            files = form.getlist("images")
            for f in files:
                if not hasattr(f, "read"):
                    continue
                data = await f.read()  # type: ignore[misc]
                images.append(
                    {
                        "fileName": getattr(f, "filename", None) or "paste.png",
                        "mimeType": getattr(f, "content_type", None) or "image/png",
                        "contentBytes": data,
                    }
                )
            return create_bug_from_template(
                template_id=str(meta.get("templateId") or ""),
                summary=str(meta.get("summary") or ""),
                description=str(meta.get("description") or ""),
                severity_id=str(meta.get("severityId") or ""),
                priority_id=str(meta.get("priorityId") or ""),
                component_versions=str(meta.get("componentVersions") or ""),
                images=images,
            )

        raw = await request.json()
        body = CreateBugRequest.model_validate(raw)
        return create_bug_from_template(
            template_id=body.templateId,
            summary=body.summary,
            description=body.description,
            severity_id=body.severityId,
            priority_id=body.priorityId,
            component_versions=body.componentVersions,
            images=[img.model_dump() for img in body.images],
        )
    except HTTPException:
        raise
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="模板不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FeishuMcpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except BaseExceptionGroup as exc:
        root = root_exception(exc)
        if isinstance(root, FeishuMcpError):
            raise HTTPException(status_code=502, detail=str(root)) from exc
        raise HTTPException(
            status_code=502, detail=f"创建 Bug 失败: {format_mcp_exception(exc)}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ValidationError):
            raise HTTPException(
                status_code=422,
                detail="; ".join(
                    f"{'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg')}"
                    for err in exc.errors()
                ),
            ) from exc
        raise


@app.get("/api/feishu/snapshot/status")
def feishu_snapshot_status() -> dict[str, Any]:
    status = read_last_status()
    status["nextRunTime"] = schedule_manager.feishu_next_run_time()
    return status


@app.post("/api/feishu/snapshot/refresh")
def feishu_snapshot_refresh(body: FeishuRefreshRequest) -> dict[str, Any]:
    try:
        return refresh_sprint(body.sprint, trigger="manual")
    except FeishuRefreshConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FeishuRefreshBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FeishuValidateError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": "快照校验失败，未覆盖 latest", "errors": exc.errors},
        ) from exc
    except FeishuMcpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except BaseExceptionGroup as exc:
        root = root_exception(exc)
        if isinstance(root, FeishuMcpError):
            raise HTTPException(status_code=502, detail=str(root)) from exc
        raise HTTPException(
            status_code=502, detail=f"飞书刷新失败: {format_mcp_exception(exc)}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        root = root_exception(exc)
        if isinstance(root, FeishuMcpError):
            raise HTTPException(status_code=502, detail=str(root)) from exc
        raise HTTPException(
            status_code=502, detail=f"飞书刷新失败: {format_mcp_exception(exc)}"
        ) from exc


@app.get("/api/modules")
def list_modules() -> list[dict[str, Any]]:
    try:
        return MeterSphereClient().list_modules()
    except MeterSphereError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/overrides/{sprint}")
def get_override(sprint: str) -> dict[str, Any]:
    return _override_with_locks(sprint)


@app.put("/api/overrides/{sprint}")
def put_override(sprint: str, body: OverridePayload) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if body.testEnv is not None:
        payload["testEnv"] = body.testEnv
    if body.riskBlock is not None:
        payload["riskBlock"] = body.riskBlock
    if body.stories is not None:
        payload["stories"] = {
            name: {k: v for k, v in fields.model_dump().items() if v is not None}
            for name, fields in body.stories.items()
        }
    if body.clearReopenManual:
        payload["reopenRows"] = None
    elif body.reopenRows is not None:
        payload["reopenRows"] = [r.model_dump() for r in body.reopenRows]
    if body.completion is not None:
        completion = body.completion.model_dump()
        overall = str(completion.get("overallResult") or "").strip()
        if overall and overall not in OVERALL_RESULT_OPTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"overallResult 无效，可选：{', '.join(OVERALL_RESULT_OPTIONS)}",
            )
        payload["completion"] = completion
    expected = body.expectedRev.model_dump(exclude_none=True) if body.expectedRev else None
    try:
        saved = save_override(sprint, payload, expected_rev=expected)
    except OverrideConflictError as exc:
        raise _conflict_http(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    saved["locks"] = get_locks(sprint)
    return saved


@app.get("/api/overrides/{sprint}/locks")
def list_override_locks(sprint: str) -> dict[str, Any]:
    return {"sprint": sprint, "locks": get_locks(sprint)}


@app.post("/api/overrides/{sprint}/locks")
def post_override_lock(sprint: str, body: OverrideLockRequest) -> dict[str, Any]:
    try:
        return acquire_lock(
            sprint,
            section=body.section,
            editor=body.editor,
            token=body.token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/overrides/{sprint}/locks/heartbeat")
def post_override_lock_heartbeat(sprint: str, body: OverrideLockRequest) -> dict[str, Any]:
    try:
        return heartbeat_lock(sprint, section=body.section, token=body.token or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/overrides/{sprint}/locks/release")
def post_override_lock_release(sprint: str, body: OverrideLockRequest) -> dict[str, Any]:
    try:
        return release_lock(sprint, section=body.section, token=body.token or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
            f"Sprint_Daily_Report_{now_beijing_mmdd()} 【{report['moduleName']}】"
        )
        mailer.send_html(subject=subject, html=html, to_emails=body.to, cc_emails=body.cc)
        return {"ok": True, "subject": subject, "rows": len(report["rows"])}
    except MeterSphereError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"发送失败: {exc}") from exc


@app.post("/api/reports/completion/preview")
def preview_completion_report(body: CompletionPreviewRequest) -> dict[str, Any]:
    try:
        report = build_completion_report(
            module_id=body.module_id,
            module_name=body.module_name,
            test_env=body.test_env,
            risk_block=body.risk_block,
        )
        html = render_completion_html(report)
        return {"report": report, "html": html}
    except MeterSphereError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/reports/completion/send")
def send_completion_report(body: CompletionSendRequest) -> dict[str, Any]:
    try:
        report = build_completion_report(
            module_id=body.module_id,
            module_name=body.module_name,
            test_env=body.test_env,
            risk_block=body.risk_block,
        )
        html = render_completion_html(report)
        subject = body.subject or (
            f"Sprint_QA_Completion_Report_{now_beijing_mmdd()} 【{report['moduleName']}】"
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

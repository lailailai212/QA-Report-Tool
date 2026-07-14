from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from .config import settings
from .email_sender import EmailSender
from .report_service import build_report, render_html
from .schedule_repo import Schedule, ScheduleRepo

logger = logging.getLogger(__name__)

# APScheduler uses mon=0..sun=6 by default in cron; we store mon=1..sun=7
_WEEKDAY_MAP = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}


class ScheduleManager:
    def __init__(self, repo: ScheduleRepo | None = None) -> None:
        self.repo = repo or ScheduleRepo()
        self.mailer = EmailSender()
        self.scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.timezone))
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.scheduler.start()
        self._started = True
        self.reload_all()
        logger.info("scheduler started")

    def shutdown(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    def reload_all(self) -> None:
        for job in self.scheduler.get_jobs():
            job.remove()
        for item in self.repo.list_all():
            if item.enabled:
                self._add_job(item)

    def upsert_job(self, item: Schedule) -> None:
        job_id = f"schedule:{item.id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        if item.enabled:
            self._add_job(item)

    def remove_job(self, schedule_id: str) -> None:
        job_id = f"schedule:{schedule_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    def _add_job(self, item: Schedule) -> None:
        hour, minute = item.time.split(":")
        days = [_WEEKDAY_MAP[d] for d in item.weekdays if d in _WEEKDAY_MAP]
        if not days:
            return
        trigger = CronTrigger(
            day_of_week=",".join(days),
            hour=int(hour),
            minute=int(minute),
            timezone=ZoneInfo(settings.timezone),
        )
        self.scheduler.add_job(
            self.run_schedule,
            trigger=trigger,
            id=f"schedule:{item.id}",
            args=[item.id],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    def next_run_time(self, schedule_id: str) -> str | None:
        job = self.scheduler.get_job(f"schedule:{schedule_id}")
        if not job or not job.next_run_time:
            return None
        return job.next_run_time.isoformat()

    def run_schedule(self, schedule_id: str) -> dict:
        item = self.repo.get(schedule_id)
        if not item:
            raise KeyError(schedule_id)
        try:
            report = build_report(
                mode="scheduled",
                module_id=item.module_id,
                module_name=item.module_name,
            )
            html = render_html(report)
            subject = item.subject_template.format(
                date=datetime.now().strftime("%m/%d"),
                module=item.module_name,
            )
            self.mailer.send_html(
                subject=subject,
                html=html,
                to_emails=item.to_emails,
                cc_emails=item.cc_emails,
            )
            self.repo.mark_run(schedule_id, "success")
            return {"ok": True, "subject": subject, "rows": len(report["rows"])}
        except Exception as exc:  # noqa: BLE001
            logger.exception("schedule run failed: %s", schedule_id)
            self.repo.mark_run(schedule_id, "failed", str(exc))
            raise


schedule_manager = ScheduleManager()

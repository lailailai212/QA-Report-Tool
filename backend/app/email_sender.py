from __future__ import annotations

import smtplib
import ssl
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable

from .config import settings


class EmailSender:
    def send_html(
        self,
        *,
        subject: str,
        html: str,
        to_emails: Iterable[str],
        cc_emails: Iterable[str] | None = None,
        inline_images: dict[str, bytes] | None = None,
    ) -> None:
        to_list = [e.strip() for e in to_emails if e and e.strip()]
        cc_list = [e.strip() for e in (cc_emails or []) if e and e.strip()]
        if not to_list:
            raise ValueError("收件人 To 不能为空")
        if not settings.smtp_host or not settings.smtp_user or not settings.smtp_password:
            raise ValueError("SMTP 未配置完整（HOST/USER/PASSWORD）")

        images = {
            cid: data
            for cid, data in (inline_images or {}).items()
            if cid and data
        }
        html_part = MIMEText(html, "html", "utf-8")
        if images:
            msg = MIMEMultipart("related")
            alt = MIMEMultipart("alternative")
            alt.attach(html_part)
            msg.attach(alt)
        else:
            msg = MIMEMultipart("alternative")
            msg.attach(html_part)
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from or settings.smtp_user
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        for cid, data in images.items():
            img = MIMEImage(data, _subtype="png")
            img["Content-ID"] = f"<{cid}>"
            img["Content-Disposition"] = f'inline; filename="{cid}.png"'
            msg.attach(img)

        recipients = to_list + cc_list
        if settings.smtp_use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(msg["From"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.starttls()
                server.login(settings.smtp_user, settings.smtp_password)
                server.sendmail(msg["From"], recipients, msg.as_string())

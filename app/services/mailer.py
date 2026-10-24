"""Email delivery architecture. Messages are always written to the outbox table; if SMTP_HOST is set the
worker/API also delivers them via SMTP. In development the body is logged so links can be copied."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.timeutil import utcnow
from app.models import EmailOutbox

logger = logging.getLogger("app.mailer")


def _send_smtp(to: str, subject: str, body: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.smtp_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:  # type: ignore[arg-type]
        smtp.starttls()
        if s.smtp_user:
            smtp.login(s.smtp_user, s.smtp_password or "")
        smtp.send_message(msg)


async def send_email(db: AsyncSession, to: str, subject: str, body: str) -> None:
    s = get_settings()
    row = EmailOutbox(to_email=to, subject=subject, body=body)
    db.add(row)
    if s.smtp_host:
        try:
            await asyncio.to_thread(_send_smtp, to, subject, body)
            row.sent_at = utcnow()
        except Exception as exc:  # delivery failures must not break the request
            row.error = str(exc)[:500]
            logger.warning("smtp delivery failed: %s", exc)
    elif not s.is_production:
        logger.info("EMAIL (dev outbox) to=%s subject=%s\n%s", to, subject, body)

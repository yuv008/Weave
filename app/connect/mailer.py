"""Plain-text mail for account links: verify, reset, invite.

``SmtpMailer`` sends through ``SMTP_HOST``/``SMTP_PORT`` (Gmail by default) with
STARTTLS and ``SMTP_USER``/``SMTP_PASSWORD``. ``MemoryMailer`` keeps messages for
tests. A failed send is logged by type only; the link is never logged.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("weave.mail")


class SmtpMailer:
    def __init__(self):
        self.host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
        self.port = int(os.environ.get("SMTP_PORT", "").strip() or 587)
        self.user = os.environ.get("SMTP_USER", "").strip()
        # Google shows app passwords in groups of four; the spaces are not part of it.
        self.password = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
        self.sender = os.environ.get("MAIL_FROM", "").strip() or self.user

    def send(self, to: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = f"Weave <{self.sender}>"
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.user, self.password)
            smtp.send_message(message)


class MemoryMailer:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, to: str, subject: str, text: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text})


def mailer_from_env():
    """An SMTP mailer when the account is set, else ``None`` (links are then not sent)."""

    if os.environ.get("SMTP_USER", "").strip() and os.environ.get("SMTP_PASSWORD", "").strip():
        return SmtpMailer()
    return None


def send_quietly(mailer, to: str, subject: str, text: str) -> None:
    if mailer is None:
        log.warning("mail not sent: no mailer is set")
        return
    try:
        mailer.send(to, subject, text)
    except Exception as exc:
        log.warning("mail not sent (%s)", type(exc).__name__)

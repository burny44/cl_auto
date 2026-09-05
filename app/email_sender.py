"""Send the processed result by email over SMTP (standard library only).

Configuration is read from the environment (see config.py / config.env):

    SMTP_HOST        e.g. smtp.gmail.com
    SMTP_PORT        e.g. 587 (STARTTLS) or 465 (SSL).  Default: 587
    SMTP_USER        login username (usually the full email address)
    SMTP_PASSWORD    login password / app password
    SMTP_SECURITY    "starttls" (default), "ssl", or "none"
    EMAIL_FROM       sender address (defaults to SMTP_USER)
    EMAIL_TO         recipient(s), comma-separated

For Gmail/Google Workspace you must use an *App Password* (with 2FA enabled),
not your normal account password.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

from . import config


class EmailError(RuntimeError):
    """Raised when the email can't be built or sent."""


def _recipients() -> list[str]:
    raw = config.get("EMAIL_TO")
    return [addr.strip() for addr in raw.replace(";", ",").split(",") if addr.strip()]


def is_configured() -> bool:
    """True if the minimum required settings are present."""

    has_sender = bool(config.get("EMAIL_FROM") or config.get("SMTP_USER"))
    return bool(config.get("SMTP_HOST")) and has_sender and bool(_recipients())


def send_email(
    subject: str,
    body: str,
    attachments: list[str] | None = None,
) -> None:
    """Send a plain-text email with optional file attachments.

    Raises EmailError on any misconfiguration or send failure.
    """

    host = config.get("SMTP_HOST")
    if not host:
        raise EmailError("SMTP_HOST is not set. Configure email in config.env.")

    user = config.get("SMTP_USER")
    password = config.get("SMTP_PASSWORD")
    sender = config.get("EMAIL_FROM") or user
    recipients = _recipients()
    port = config.get_int("SMTP_PORT", 587)
    security = (config.get("SMTP_SECURITY") or "starttls").lower()

    if not sender:
        raise EmailError("No sender address. Set EMAIL_FROM or SMTP_USER.")
    if not recipients:
        raise EmailError("No recipients. Set EMAIL_TO (comma-separated).")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    for path in attachments or []:
        if not path or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        msg.add_attachment(
            data,
            maintype="text",
            subtype="plain",
            filename=os.path.basename(path),
        )

    try:
        if security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
                if user:
                    server.login(user, password)
                server.send_message(msg, from_addr=sender, to_addrs=recipients)
        else:
            with smtplib.SMTP(host, port, timeout=60) as server:
                server.ehlo()
                if security != "none":
                    context = ssl.create_default_context()
                    server.starttls(context=context)
                    server.ehlo()
                if user:
                    server.login(user, password)
                server.send_message(msg, from_addr=sender, to_addrs=recipients)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Failed to send email via {host}:{port}: {exc}") from exc

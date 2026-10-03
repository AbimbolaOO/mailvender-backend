"""Handing messages to Postfix (which passes them through the OpenDKIM milter)."""

import smtplib
import ssl
from typing import Protocol

from app.config import get_settings


class TemporaryDeliveryError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class PermanentDeliveryError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class MailTransport(Protocol):
    def send(self, envelope_from: str, recipients: list[str], message: bytes) -> str:
        """Returns the MTA's response (e.g. "250 2.0.0 Ok: queued as ABC123")."""
        ...


class SmtpTransport:
    def __init__(self) -> None:
        self.settings = get_settings()

    def send(self, envelope_from: str, recipients: list[str], message: bytes) -> str:
        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
                smtp.ehlo()
                if self.settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                refused = smtp.sendmail(envelope_from, recipients, message)
                if refused:
                    code, text = next(iter(refused.values()))
                    error = PermanentDeliveryError if code >= 500 else TemporaryDeliveryError
                    raise error(text.decode(errors="replace"), code)
                code, text = smtp.noop()
                return f"{code} {text.decode(errors='replace')}"
        except smtplib.SMTPRecipientsRefused as error:
            code, text = next(iter(error.recipients.values()))
            cls = PermanentDeliveryError if code >= 500 else TemporaryDeliveryError
            raise cls(text.decode(errors="replace"), code) from error
        except smtplib.SMTPResponseException as error:
            cls = PermanentDeliveryError if error.smtp_code >= 500 else TemporaryDeliveryError
            raise cls(str(error.smtp_error), error.smtp_code) from error
        except (OSError, smtplib.SMTPException) as error:
            raise TemporaryDeliveryError(str(error)) from error

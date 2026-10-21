"""Alerts: tell someone when the platform needs attention, instead of waiting for them to look.

The `alerts` job runs the admin dashboard's health checks (port 25, jobs, queue, platform DKIM,
backups, blocklists – see AdminDashboardService.checks) every 10 minutes. When a check turns red
(`problem`), it emails ALERT_EMAILS (blank = the active admins) and posts to ALERT_WEBHOOK_URL
(Slack/Discord/Teams-style `{"text": …}`), once. When it's fixed, a "fixed" message follows. While
problems stay open, a reminder goes out once a day. Warnings stay on the dashboard only.

Email alerts go through our own Postfix, so they can't arrive when outgoing mail itself is broken
(port 25 blocked, worker down) – that's what the webhook is for.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.config import get_settings
from app.logging import get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services import emails
from app.services.admin_dashboard import AdminDashboardService, Check, Port25Probe, port25_open
from app.services.common import enqueue_system_email
from app.services.storage import ObjectStorage

log = get_logger("alerts")

STATE_KEY = "alerts"
REMIND_EVERY = timedelta(hours=24)

Post = Callable[[str, dict[str, Any]], None]


def post_webhook(url: str, payload: dict[str, Any]) -> None:
    httpx.post(url, json=payload, timeout=10).raise_for_status()


class AlertService:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage, port25: Port25Probe = port25_open,
                 post: Post = post_webhook):
        self.uow = uow
        self.storage = storage
        self.port25 = port25
        self.post = post
        self.settings = get_settings()

    def recipients(self) -> list[str]:
        return self.settings.alert_email_list or [a.email for a in self.uow.admins.list_all() if not a.disabled_at]

    def run(self) -> int:
        """Returns how many alerts went out (0 when nothing changed)."""
        now = utcnow()
        checks = AdminDashboardService(self.uow, self.storage, self.port25).checks()
        failing = {(c.key or c.title): c for c in checks if c.status == "problem"}
        state = self.uow.system_state.get(STATE_KEY) or {}
        open_: dict[str, dict[str, str]] = state.get("open", {})
        new = [check for key, check in failing.items() if key not in open_]
        fixed = [item["title"] for key, item in open_.items() if key not in failing]
        reminded = state.get("reminded_at")
        remind = bool(failing) and not new and not fixed and reminded is not None and \
            now - datetime.fromisoformat(reminded) >= REMIND_EVERY

        next_open = {key: open_.get(key, {"title": check.title, "since": now.isoformat()}) | {"title": check.title}
                     for key, check in failing.items()}
        sent = 0
        if new or fixed or remind:
            still = [check for key, check in failing.items() if key in open_]
            sent = self._send(new, fixed, still, remind)
            reminded = now.isoformat()
        elif not failing:
            reminded = None
        self.uow.system_state.put(STATE_KEY, {"open": next_open, "reminded_at": reminded}, now)
        self.uow.commit()
        return sent

    def _send(self, new: list[Check], fixed: list[str], still: list[Check], remind: bool) -> int:
        if new:
            subject = f"Mailvender: {new[0].title}" if len(new) == 1 else f"Mailvender: {len(new)} problems need attention"
        elif remind:
            subject = f"Mailvender: still {len(still)} problem{'s' if len(still) != 1 else ''} (daily reminder)"
        else:
            subject = f"Mailvender: fixed – {fixed[0]}" if len(fixed) == 1 else f"Mailvender: {len(fixed)} problems fixed"
        message = emails.alert(subject, [(c.title, c.detail) for c in new], fixed,
                               [(c.title, c.detail) for c in still], self.settings.admin_app_url)
        sent = 0
        for address in self.recipients():
            enqueue_system_email(self.uow, address, message, kind="alert")
            sent += 1
        if self.settings.alert_webhook_url:
            try:
                self.post(self.settings.alert_webhook_url, {"text": message.text})
                sent += 1
            except Exception as error:  # noqa: BLE001 - a webhook outage must not stop email alerts
                log_event(log, "alerts.webhook_failed", logging.WARNING, error=str(error)[:300])
        log_event(log, "alerts.sent", subject=subject, new=len(new), fixed=len(fixed), reminder=remind)
        return sent

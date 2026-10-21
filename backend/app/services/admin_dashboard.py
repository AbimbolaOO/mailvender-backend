"""The admin dashboard's numbers: daily usage, spam and bounce rates, and system checks.

Everything is counted in the database (one query per number or per daily series); no
customer content is read. Days are UTC days.
"""

import socket
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import get_settings
from app.logging import get_logger
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.reputation import DELIST
from app.services.reputation import STATE_KEY as REPUTATION_KEY
from app.services.storage import ObjectStorage

log = get_logger("admin_dashboard")

Port25Probe = Callable[[], bool | None]

# How often each worker job runs (worker.py); a job silent for 3× its interval is reported.
JOB_INTERVALS = {
    "dns_recheck": timedelta(minutes=10),
    "platform_dkim": timedelta(minutes=5),
    "account_health": timedelta(minutes=5),
    "retention": timedelta(hours=24),
    "dkim_key_sync": timedelta(seconds=30),
    "reputation": timedelta(hours=1),
    "alerts": timedelta(minutes=10),
}

PROVIDERS: dict[str, str] = {
    "gmail.com": "Gmail", "googlemail.com": "Gmail",
    "outlook.com": "Outlook / Hotmail", "hotmail.com": "Outlook / Hotmail", "live.com": "Outlook / Hotmail",
    "msn.com": "Outlook / Hotmail", "hotmail.co.uk": "Outlook / Hotmail", "outlook.fr": "Outlook / Hotmail",
    "yahoo.com": "Yahoo / AOL", "ymail.com": "Yahoo / AOL", "aol.com": "Yahoo / AOL",
    "yahoo.co.uk": "Yahoo / AOL", "rocketmail.com": "Yahoo / AOL",
    "icloud.com": "Apple iCloud", "me.com": "Apple iCloud", "mac.com": "Apple iCloud",
}
PROVIDER_ORDER = ["Gmail", "Outlook / Hotmail", "Yahoo / AOL", "Apple iCloud", "Other"]

BOUNCE_MEANINGS = {
    "5.1.1": "The mailbox doesn't exist (a typo or an old address).",
    "5.1.10": "The address doesn't exist.",
    "5.1.2": "The domain doesn't exist or doesn't take email.",
    "5.2.1": "The mailbox is switched off.",
    "5.2.2": "The mailbox is full.",
    "5.4.1": "The receiving server refused the address.",
    "5.7.1": "Blocked by the receiver's spam or policy rules.",
    "5.7.25": "The receiver wants our server to have reverse DNS.",
    "5.7.26": "Failed SPF/DKIM/DMARC checks.",
    "5.7.27": "The sender's domain doesn't accept mail.",
}

# Plain-words versions of common delivery errors (worker last_error strings, SMTP replies).
ERROR_MEANINGS = [
    (("timed out", "network is unreachable", "connection refused", "no route to host"),
     "Our mail server couldn't reach theirs – is outbound port 25 blocked?"),
    (("user unknown", "does not exist", "no such user", "5.1.1"),
     "The recipient's mailbox doesn't exist."),
    (("mailbox full", "quota", "5.2.2"), "The recipient's mailbox is full."),
    (("spam", "blocked", "blacklist", "blocklist", "5.7.1"),
     "The receiver blocked the message as spam or by policy."),
    (("suspended", "account_suspended"), "Sending was stopped because the account is suspended."),
]


def plain_error(text: str | None) -> str:
    if not text:
        return "No error message was recorded."
    lowered = text.lower()
    for needles, meaning in ERROR_MEANINGS:
        if any(needle in lowered for needle in needles):
            return meaning
    return "The delivery failed. The technical message is shown below."


def bounce_meaning(code: str | None) -> str:
    if not code:
        return "The receiver gave no reason."
    if code in BOUNCE_MEANINGS:
        return BOUNCE_MEANINGS[code]
    if code.startswith("4."):
        return "A temporary problem at the receiver; we retried."
    return "The receiver permanently refused the message."


def provider_of(domain: str) -> str:
    domain = domain.lower()
    if domain in PROVIDERS:
        return PROVIDERS[domain]
    if domain.startswith(("hotmail.", "outlook.", "live.")):
        return "Outlook / Hotmail"
    if domain.startswith("yahoo."):
        return "Yahoo / AOL"
    return "Other"


def rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def spam_level(complaint_rate: float) -> str:
    """Gmail's guidance: stay below 0.1%; above 0.3% mail starts being rejected."""
    return "good" if complaint_rate < 0.001 else ("warning" if complaint_rate < 0.003 else "bad")


def start_of_day(at: datetime) -> datetime:
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


# ---- Outbound port 25 (Hetzner blocks it on new servers until asked) ----

_port25: dict[str, Any] = {"checked": 0.0, "open": None}


def port25_open() -> bool | None:
    """Can this server open SMTP connections to Gmail? Cached for 10 minutes; None = couldn't tell."""
    if time.monotonic() - _port25["checked"] < 600:
        return _port25["open"]
    try:
        with socket.create_connection(("gmail-smtp-in.l.google.com", 25), timeout=4):
            result: bool | None = True
    except TimeoutError:
        result = False
    except OSError as error:
        result = None if isinstance(error, socket.gaierror) else False
    _port25.update(checked=time.monotonic(), open=result)
    return result


# ---- A small cache, so a busy dashboard doesn't recount every few seconds ----

@dataclass
class _Cache:
    entries: dict[tuple[Any, ...], tuple[float, Any]] = field(default_factory=dict)

    def get(self, key: tuple[Any, ...], ttl: int, compute: Callable[[], Any]) -> Any:
        if ttl > 0:
            hit = self.entries.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        value = compute()
        if ttl > 0:
            self.entries[key] = (time.monotonic(), value)
        return value


cache = _Cache()


def checked_ago(value: str | None, now: datetime) -> str:
    if not value:
        return ""
    minutes = int((now - datetime.fromisoformat(value)).total_seconds() // 60)
    return "just now" if minutes < 2 else f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} hours ago"


@dataclass
class Check:
    status: str  # ok | warning | problem
    title: str
    detail: str
    key: str = ""  # stable across runs (the title can contain numbers) – alerts compare by it


class AdminDashboardService:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage, port25: Port25Probe = port25_open):
        self.uow = uow
        self.storage = storage
        self.port25 = port25
        self.settings = get_settings()

    def _cached(self, key: tuple[Any, ...], compute: Callable[[], Any]) -> Any:
        return cache.get(key, self.settings.admin_stats_cache_seconds, compute)

    # ---- Overview ----

    def overview(self, days: int) -> dict[str, Any]:
        return self._cached(("overview", days), lambda: self._overview(days))

    def _overview(self, days: int) -> dict[str, Any]:
        stats = self.uow.admin_stats
        today = start_of_day(utcnow())
        tomorrow, yesterday = today + timedelta(days=1), today - timedelta(days=1)
        cards = []
        for metric, label, explain in (
            ("sent", "Emails sent", "Emails our customers asked us to send."),
            ("delivered", "Delivered", "Accepted by the receiver's mail server."),
            ("bounced", "Bounced", "Came back undelivered."),
            ("complaints", "Spam complaints", "Someone clicked “Report spam”."),
            ("signups", "New signups", "People who created an account."),
            ("active_users", "Active users", "People who used Mailvender."),
        ):
            cards.append({"key": metric, "label": label, "explain": explain,
                          "today": stats.count_between(metric, today, tomorrow),
                          "yesterday": stats.count_between(metric, yesterday, today)})
        return {"cards": cards, "series": self._series(days), "checks": [c.__dict__ for c in self.checks()],
                "limits": self._limits(), "generated_at": utcnow()}

    def _limits(self) -> dict[str, float]:
        return {"bounce_rate": self.settings.health_max_bounce_rate,
                "complaint_rate": self.settings.health_max_complaint_rate,
                "gmail_spam_warning": 0.001, "gmail_spam_limit": 0.003}

    def _series(self, days: int) -> list[dict[str, Any]]:
        stats = self.uow.admin_stats
        first = start_of_day(utcnow()) - timedelta(days=days - 1)
        metrics = ("sent", "delivered", "bounced", "hard_bounces", "complaints", "failed", "signups",
                   "active_users")
        counts = {metric: stats.daily_counts(metric, first) for metric in metrics}
        categories = stats.daily_categories(first)
        series = []
        for offset in range(days):
            day = (first + timedelta(days=offset)).date()
            row: dict[str, Any] = {"day": day.isoformat()}
            for metric in metrics:
                row[metric] = counts[metric].get(day, 0)
            row["transactional"] = categories.get((day, "transactional"), 0)
            row["bulk"] = categories.get((day, "bulk"), 0)
            row["bounce_rate"] = rate(row["hard_bounces"], row["delivered"])
            row["complaint_rate"] = rate(row["complaints"], row["delivered"])
            series.append(row)
        return series

    # ---- Spam and deliverability ----

    def deliverability(self, days: int) -> dict[str, Any]:
        return self._cached(("deliverability", days), lambda: self._deliverability(days))

    def _window(self, start: datetime, end: datetime, label: str) -> dict[str, Any]:
        stats = self.uow.admin_stats
        delivered = stats.count_between("delivered", start, end)
        complaints = stats.count_between("complaints", start, end)
        hard = stats.count_between("hard_bounces", start, end)
        complaint_rate = rate(complaints, delivered)
        return {"label": label, "delivered": delivered, "complaints": complaints, "hard_bounces": hard,
                "complaint_rate": complaint_rate, "bounce_rate": rate(hard, delivered),
                "spam_level": spam_level(complaint_rate)}

    def _deliverability(self, days: int) -> dict[str, Any]:
        stats = self.uow.admin_stats
        now = utcnow()
        since = now - timedelta(days=days)
        windows = [
            self._window(start_of_day(now), now + timedelta(seconds=1), "Today"),
            self._window(now - timedelta(days=7), now + timedelta(seconds=1), "Last 7 days"),
            self._window(now - timedelta(days=30), now + timedelta(seconds=1), "Last 30 days"),
        ]

        providers: dict[str, dict[str, int]] = defaultdict(lambda: {"delivered": 0, "hard_bounces": 0,
                                                                    "soft_bounces": 0, "complaints": 0})
        for domain, count in stats.recipient_domains(since, 2000):
            providers[provider_of(domain)]["delivered"] += count
        for domain, type_, classification, count in stats.event_domains(since):
            bucket = providers[provider_of(domain)]
            if type_ == "complaint":
                bucket["complaints"] += count
            elif classification == "hard":
                bucket["hard_bounces"] += count
            else:
                bucket["soft_bounces"] += count
        by_provider = [
            {"provider": name, **values, "bounce_rate": rate(values["hard_bounces"], values["delivered"]),
             "complaint_rate": rate(values["complaints"], values["delivered"]),
             "spam_level": spam_level(rate(values["complaints"], values["delivered"]))}
            for name in PROVIDER_ORDER if (values := providers.get(name))
        ]

        rates = [
            {"account_id": account_id, "delivered": accepted, "hard_bounces": hard, "complaints": complaints,
             "bounce_rate": rate(hard, accepted), "complaint_rate": rate(complaints, accepted)}
            for account_id, accepted, hard, complaints in stats.account_rates(since) if accepted
        ]
        rates.sort(key=lambda r: (r["complaint_rate"], r["bounce_rate"], r["delivered"]), reverse=True)
        worst = rates[:10]
        accounts = stats.accounts_by_ids([r["account_id"] for r in worst])
        owners = stats.owner_emails([r["account_id"] for r in worst])
        for row in worst:
            account = accounts.get(row["account_id"])
            row["account_name"] = account.name if account else "Deleted account"
            row["owner_email"] = owners.get(row["account_id"])
            row["suspended"] = bool(account and account.sending_suspended_at)

        reasons = [{"status_code": code, "count": count, "meaning": bounce_meaning(code),
                    "example": (sample or "")[:300] or None}
                   for code, count, sample in stats.bounce_reasons(since, 10)]

        known = self.settings.known_sending_ip_set
        reporters = [{**row, "reporter": row.pop("key"), "pass_rate": rate(row["dmarc_pass"], row["messages"])}
                     for row in self.uow.inbound.dmarc_by_reporter(since)]
        sources = []
        for row in self.uow.inbound.dmarc_by_source(since):
            ip = row.pop("key")
            ours = ip in known
            sources.append({**row, "source_ip": ip, "ours": ours, "pass_rate": rate(row["dmarc_pass"], row["messages"]),
                            "suspicious": (bool(known) and not ours) or row["dmarc_pass"] == 0})
        series = [{"day": r["day"], "delivered": r["delivered"], "bounce_rate": r["bounce_rate"],
                   "complaint_rate": r["complaint_rate"]} for r in self._series(days)]
        return {"windows": windows, "by_provider": by_provider, "worst_accounts": worst,
                "bounce_reasons": reasons, "dmarc_reporters": reporters, "dmarc_sources": sources,
                "known_sending_ips": sorted(known), "series": series, "limits": self._limits(),
                "generated_at": now}

    # ---- System ----

    def checks(self) -> list[Check]:
        """What needs attention, worst first (also the Overview's “Is everything OK?” card)."""
        now = utcnow()
        checks: list[Check] = []

        port = self.port25()
        if port is False:
            checks.append(Check("problem", "Outbound port 25 is blocked",
                                "No email can reach Gmail, Outlook and others. Ask Hetzner to unblock port 25 "
                                "(PRODUCTION_SETUP step 2).", "port25"))
        elif port is None:
            checks.append(Check("warning", "Couldn't test outbound port 25", "The DNS lookup for Gmail failed.", "port25"))
        else:
            checks.append(Check("ok", "Outbound port 25 is open", "Our server can reach other mail servers.", "port25"))

        runs = {run.name: run for run in self.uow.jobs.list_runs()}
        if not runs:
            checks.append(Check("problem", "The worker has never run",
                                "Background jobs and sending need the worker container.", "worker"))
        for name, run in sorted(runs.items()):
            interval = JOB_INTERVALS.get(name)
            if run.last_error:
                checks.append(Check("problem", f"Job “{name}” failed", run.last_error.strip().splitlines()[-1][:300], f"job:{name}"))
            elif interval and run.last_finished_at and now - run.last_finished_at > interval * 3 + timedelta(minutes=5):
                checks.append(Check("problem", f"Job “{name}” has stopped running",
                                    f"Last finished {run.last_finished_at:%Y-%m-%d %H:%M} UTC. Is the worker up?",
                                    f"job:{name}"))

        outbox = self.uow.admin_stats.outbox_summary()
        dead = sum(count for _, status, count in outbox if status == "dead")
        if dead:
            checks.append(Check("warning", f"{dead} queued job(s) gave up",
                                "They failed 10 times. See System → Queue for which kind.", "outbox_dead"))
        oldest = self.uow.admin_stats.oldest_pending_outbox()
        if oldest and now - oldest > timedelta(minutes=15):
            minutes = int((now - oldest).total_seconds() // 60)
            checks.append(Check("warning", "The sending queue is slow", f"The oldest waiting job is {minutes} min old.",
                                "queue_slow"))

        keys = self.uow.platform_dkim_keys.list_for_domain(self.settings.platform_domain)
        if not any(key.status == "active" for key in keys):
            checks.append(Check("problem", "Mailvender's own DKIM key isn't active",
                                "System emails aren't signed. Run app.cli platform-dkim (PRODUCTION_SETUP step 12).",
                                "platform_dkim"))

        suspended = self.uow.admin_stats.count_audit_since("account.suspended", start_of_day(now))
        if suspended:
            checks.append(Check("warning", f"{suspended} account(s) suspended today", "See Needs help.", "suspended_today"))

        unread_abuse = self.uow.inbound.unread_counts().get("abuse", 0)
        if unread_abuse:
            checks.append(Check("warning", f"{unread_abuse} unread message(s) at abuse@",
                                "Someone may be reporting one of our customers.", "abuse_unread"))

        for label, age, limit in (("Database backup", self._backup_age("backups/db/"), timedelta(hours=7)),
                                  ("Log backup", self._backup_age("logs/api/"), timedelta(hours=2))):
            if age is None:
                checks.append(Check("warning", f"{label}: none found in the last week",
                                    "Check the backup timers (PRODUCTION_SETUP step 14).", f"backup:{label}"))
            elif age > limit:
                checks.append(Check("problem" if label.startswith("Database") else "warning",
                                    f"{label} is {int(age.total_seconds() // 3600)} hours old",
                                    "Check the backup timers (PRODUCTION_SETUP step 14).", f"backup:{label}"))
        checks.extend(self._blocklist_checks(now))
        order = {"problem": 0, "warning": 1, "ok": 2}
        return sorted(checks, key=lambda c: order[c.status])

    def _blocklist_checks(self, now: datetime) -> list[Check]:
        """From the hourly `reputation` job (services/reputation.py)."""
        state = self.uow.system_state.get(REPUTATION_KEY)
        if not state:
            if self.settings.known_sending_ip_set:
                return [Check("warning", "Blocklists haven't been checked yet",
                              "The worker checks our IP and domain every hour.", "blocklists")]
            return [Check("warning", "Blocklists aren't checked",
                          "Set KNOWN_SENDING_IPS (our server's IP) so the worker can check them.", "blocklists")]
        checks = []
        results = state.get("results", [])
        for item in results:
            if item["status"] == "listed":
                since = item.get("since", "")[:10]
                checks.append(Check(
                    "problem", f"{item['target']} is on the {item['zone']} blocklist",
                    f"Listed since {since}. Mail from it is likely being rejected. Find the customer who caused it "
                    f"(Spam & delivery), then ask for removal: {DELIST.get(item['zone'], item['zone'])}",
                    f"blocklist:{item['zone']}:{item['target']}"))
        if not checks:
            unknown = sorted({item["zone"] for item in results if item["status"] == "unknown"})
            if unknown and len(unknown) == len({item["zone"] for item in results}):
                checks.append(Check("warning", "Blocklists couldn't be checked",
                                    "The lists refused our server's DNS queries (a public resolver?). See "
                                    "RUNBOOK “Blocklists”.", "blocklists"))
            else:
                checks.append(Check("ok", "Not on any blocklist",
                                    f"Checked {len(results)} lists {checked_ago(state.get('checked_at'), now)}.",
                                    "blocklists"))
        return checks

    def _latest_object(self, folder: str) -> tuple[str, int, datetime] | None:
        """The newest object under <prefix><folder><YYYY>/<MM>/<DD>/, looking back a week."""
        today = utcnow()
        try:
            for back in range(8):
                day = today - timedelta(days=back)
                items = self.storage.list(f"{self.settings.aws_s3_prefix}{folder}{day:%Y/%m/%d}/")
                if items:
                    newest = max(items, key=lambda item: item.last_modified)
                    return newest.key, newest.size, newest.last_modified
        except Exception:  # noqa: BLE001 - a storage hiccup must not break the dashboard
            log.warning("admin.backup_check_failed", exc_info=True, extra={"event": "admin.backup_check_failed"})
        return None

    def _backup_age(self, folder: str) -> timedelta | None:
        latest = self._latest_object(folder)
        return utcnow() - latest[2] if latest else None

    def system(self) -> dict[str, Any]:
        return self._cached(("system",), self._system)

    def _system(self) -> dict[str, Any]:
        now = utcnow()
        jobs = []
        for run in self.uow.jobs.list_runs():
            interval = JOB_INTERVALS.get(run.name)
            stale = bool(interval and run.last_finished_at
                         and now - run.last_finished_at > interval * 3 + timedelta(minutes=5))
            jobs.append({"name": run.name, "last_started_at": run.last_started_at,
                         "last_finished_at": run.last_finished_at, "last_error": run.last_error,
                         "every_seconds": int(interval.total_seconds()) if interval else None,
                         "ok": not run.last_error and not stale})
        queue = [{"kind": kind, "status": status, "count": count}
                 for kind, status, count in sorted(self.uow.admin_stats.outbox_summary())]
        oldest = self.uow.admin_stats.oldest_pending_outbox()
        dkim = [{"selector": key.selector, "status": key.status, "created_at": key.created_at,
                 "activated_at": key.activated_at}
                for key in self.uow.platform_dkim_keys.list_for_domain(self.settings.platform_domain)
                if key.status != "retired"]
        backups = []
        for label, folder in (("Database", "backups/db/"), ("Logs (api)", "logs/api/")):
            latest = self._latest_object(folder)
            backups.append({"label": label, "key": latest[0] if latest else None,
                            "size": latest[1] if latest else None, "at": latest[2] if latest else None})
        return {"jobs": jobs, "queue": queue, "oldest_pending_at": oldest, "platform_domain": self.settings.platform_domain,
                "platform_dkim": dkim, "backups": backups, "port25_open": self.port25(),
                "checks": [c.__dict__ for c in self.checks()], "generated_at": now}

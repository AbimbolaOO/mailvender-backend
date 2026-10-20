""""Needs help": customers who are stuck or causing problems, each with a plain-words fix.

Problems are worked out from the data every time (nothing is stored twice). An admin can mark a
problem "contacted" or "resolved" with a note (`admin_issue_states`); a resolved problem stays
hidden for a day, then comes back if the condition is still true.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app import models as m
from app.config import get_settings
from app.errors import ValidationFailed, field_error
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.admin_auth import AdminAuth, admin_audit
from app.services.admin_dashboard import plain_error, rate
from app.services.admin_customers import NoDnsLookups
from app.services.domains import DomainService

LIMIT_PER_TYPE = 200

# type: (severity, label shown on the filter chip)
TYPES: dict[str, tuple[str, str]] = {
    "suspended": ("high", "Sending suspended"),
    "near_limit": ("high", "Close to being suspended"),
    "failed_deliveries": ("medium", "Emails failing"),
    "hitting_limits": ("medium", "Hitting sending limits"),
    "domain_broken": ("medium", "Domain stopped working"),
    "domain_pending": ("low", "Domain not verified"),
    "sender_pending": ("low", "Sender waiting for approval"),
    "bulk_no_address": ("low", "No postal address"),
    "unverified_signup": ("low", "Never verified email"),
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Issue:
    key: str
    type: str
    who: str
    problem: str
    how_to_fix: str
    since: datetime | None
    user_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    contact_email: str | None = None
    details: list[str] = field(default_factory=list)
    action: str | None = None  # resend_verification | reinstate | change_limits
    state: str = "open"  # open | contacted | resolved
    note: str | None = None
    state_updated_at: datetime | None = None
    reopened: bool = False

    @property
    def severity(self) -> str:
        return TYPES[self.type][0]

    @property
    def label(self) -> str:
        return TYPES[self.type][1]


class AdminIssuesService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow
        self.settings = get_settings()

    def find(self, type_: str | None = None) -> tuple[list[Issue], dict[str, int]]:
        issues = self._detect()
        states = self.uow.admin_issues.get_many([i.key for i in issues])
        now = utcnow()
        visible: list[Issue] = []
        for issue in issues:
            state = states.get(issue.key)
            if state:
                issue.note, issue.state_updated_at = state.note, state.updated_at
                if state.status == "resolved" and now - state.updated_at < timedelta(days=1):
                    continue  # hidden for a day
                if state.status == "resolved":
                    issue.reopened = True  # still true a day later
                else:
                    issue.state = state.status
            visible.append(issue)
        counts = {name: 0 for name in TYPES}
        for issue in visible:
            counts[issue.type] += 1
        if type_:
            visible = [i for i in visible if i.type == type_]
        visible.sort(key=lambda i: (SEVERITY_ORDER[i.severity], i.state == "contacted",
                                    -(i.since.timestamp() if i.since else 0)))
        return visible, counts

    def set_state(self, auth: AdminAuth, key: str, status: str, note: str | None) -> None:
        kind, _, subject = key.partition(":")
        if kind not in TYPES or not subject:
            raise ValidationFailed([field_error("key", "Unknown problem.", "invalid")])
        if status == "open":
            self.uow.admin_issues.delete(key)
        else:
            self.uow.admin_issues.upsert(key, status, note, auth.admin.id, utcnow())
        admin_audit(self.uow, auth, f"admin.issue_{status}", target_type="issue", reason=note, issue=key)
        self.uow.commit()

    # ---- Detection ----

    def _owners(self, account_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        return self.uow.admin_stats.owner_emails(list(set(account_ids)))

    def _who(self, account: m.Account | None, owner: str | None) -> str:
        name = account.name if account else "Unknown account"
        return f"{owner} ({name})" if owner else name

    def _detect(self) -> list[Issue]:
        stats = self.uow.admin_stats
        now = utcnow()
        two_days_ago = now - timedelta(days=2)
        issues: list[Issue] = []

        for user in stats.unverified_users(two_days_ago, LIMIT_PER_TYPE):
            issues.append(Issue(
                key=f"unverified_signup:{user.id}", type="unverified_signup", who=user.email,
                problem="Signed up but never clicked the verification link.",
                how_to_fix="They probably missed the email. Ask them to check their spam folder, or send a "
                           "new link with the button.",
                since=user.created_at, user_id=user.id, contact_email=user.email, action="resend_verification"))

        suspended = stats.suspended_accounts(LIMIT_PER_TYPE)
        owners = self._owners([a.id for a in suspended])
        for account in suspended:
            issues.append(Issue(
                key=f"suspended:{account.id}", type="suspended", who=self._who(account, owners.get(account.id)),
                problem=f"Sending is suspended: {account.suspension_reason or 'no reason recorded'}.",
                how_to_fix="Open the account and look at its recent bounces and complaints. If they cleaned "
                           "their list (or it was a mistake), reinstate sending. Otherwise keep it suspended "
                           "and tell them why.",
                since=account.sending_suspended_at, account_id=account.id, contact_email=owners.get(account.id),
                action="reinstate"))
        suspended_ids = {a.id for a in suspended}

        window = now - timedelta(days=self.settings.health_window_days)
        max_bounce, max_complaint = self.settings.health_max_bounce_rate, self.settings.health_max_complaint_rate
        near = []
        for account_id, accepted, hard, complaints in stats.account_rates(window):
            if account_id in suspended_ids or accepted < max(10, self.settings.health_min_volume // 4):
                continue
            bounce_rate, complaint_rate = rate(hard, accepted), rate(complaints, accepted)
            if bounce_rate > 0.7 * max_bounce or complaint_rate > 0.7 * max_complaint:
                near.append((account_id, accepted, bounce_rate, complaint_rate))
        accounts = stats.accounts_by_ids([n[0] for n in near])
        owners = self._owners([n[0] for n in near])
        for account_id, accepted, bounce_rate, complaint_rate in near:
            account = accounts.get(account_id)
            if account is None or account.status != "active":
                continue
            issues.append(Issue(
                key=f"near_limit:{account_id}", type="near_limit", who=self._who(account, owners.get(account_id)),
                problem=f"Bounce rate {bounce_rate:.1%} (limit {max_bounce:.0%}) and spam complaints "
                        f"{complaint_rate:.2%} (limit {max_complaint:.1%}) over {accepted} emails.",
                how_to_fix="They're probably sending to an old or bought list. Contact them before we suspend "
                           "them automatically: ask where the addresses came from and to remove people who "
                           "never opened.",
                since=None, account_id=account_id, contact_email=owners.get(account_id)))

        failing = stats.dead_deliveries(now - timedelta(days=7))
        accounts = stats.accounts_by_ids([f[0] for f in failing])
        owners = self._owners([f[0] for f in failing])
        for account_id, count, last_error in failing:
            account = accounts.get(account_id)
            if account is None or account.status != "active":
                continue
            issues.append(Issue(
                key=f"failed_deliveries:{account_id}", type="failed_deliveries",
                who=self._who(account, owners.get(account_id)),
                problem=f"{count} email(s) failed in the last 7 days. {plain_error(last_error)}",
                how_to_fix="If it's our server (port 25, the queue), fix that first – see System. If the "
                           "receiver refused them, the addresses are probably bad; they're suppressed "
                           "automatically.",
                since=None, account_id=account_id, contact_email=owners.get(account_id),
                details=[last_error[:300]] if last_error else []))

        hits = [(a, n) for a, n in stats.limit_violations(now - timedelta(days=1)) if n >= 3]
        accounts = stats.accounts_by_ids([h[0] for h in hits])
        owners = self._owners([h[0] for h in hits])
        for account_id, count in hits:
            account = accounts.get(account_id)
            if account is None or account.status != "active":
                continue
            issues.append(Issue(
                key=f"hitting_limits:{account_id}", type="hitting_limits",
                who=self._who(account, owners.get(account_id)),
                problem=f"Went over their sending limit {count} times in the last 24 hours "
                        f"({account.hourly_recipient_limit}/hour, {account.daily_recipient_limit}/day).",
                how_to_fix="If they're a genuine sender with clean lists, raise their limits. If not, they "
                           "should send more slowly. After "
                           f"{self.settings.limit_violations_before_suspension} attempts they're suspended "
                           "automatically.",
                since=None, account_id=account_id, contact_email=owners.get(account_id), action="change_limits"))

        domains = stats.pending_domains(two_days_ago, LIMIT_PER_TYPE)
        owners = self._owners([d.account_id for d in domains])
        accounts = stats.accounts_by_ids([d.account_id for d in domains])
        service = DomainService(self.uow, NoDnsLookups())
        for domain in domains:
            view = service.view(domain)
            wrong = [r for r in view.records if r.required and r.status != "verified"]
            details = [f"{r.type} {r.name} → {r.value} ({r.status})" for r in wrong]
            broken = domain.verified_at is not None
            issues.append(Issue(
                key=f"{'domain_broken' if broken else 'domain_pending'}:{domain.id}",
                type="domain_broken" if broken else "domain_pending",
                who=self._who(accounts.get(domain.account_id), owners.get(domain.account_id)),
                problem=(f"{domain.name} was verified, but its DNS records changed – it can't send now."
                         if broken else f"{domain.name} was added but its DNS records aren't right yet."),
                how_to_fix="Send them the records below (also shown in their Settings → Sending domains). "
                           "DNS changes can take up to a few hours; then they click Verify.",
                since=domain.last_checked_at if broken else domain.created_at, account_id=domain.account_id,
                contact_email=owners.get(domain.account_id), details=details))

        senders = stats.pending_senders(now - timedelta(days=1), LIMIT_PER_TYPE)
        owners = self._owners([s.account_id for s in senders])
        accounts = stats.accounts_by_ids([s.account_id for s in senders])
        for sender in senders:
            issues.append(Issue(
                key=f"sender_pending:{sender.id}", type="sender_pending",
                who=self._who(accounts.get(sender.account_id), owners.get(sender.account_id)),
                problem=f"The sender address {sender.email} is waiting for approval.",
                how_to_fix=f"Someone with access to {sender.email} needs to approve it in Settings → Senders.",
                since=sender.created_at, account_id=sender.account_id,
                contact_email=owners.get(sender.account_id)))

        no_address = stats.bulk_without_address(LIMIT_PER_TYPE)
        owners = self._owners([a.id for a in no_address])
        for account in no_address:
            issues.append(Issue(
                key=f"bulk_no_address:{account.id}", type="bulk_no_address",
                who=self._who(account, owners.get(account.id)),
                problem="Their domain can send newsletters (bulk), but the account has no postal address.",
                how_to_fix="The law requires a postal address in bulk email, so bulk sends are refused. Ask "
                           "them to add it in Settings → Sending.",
                since=account.created_at, account_id=account.id, contact_email=owners.get(account.id)))
        return issues

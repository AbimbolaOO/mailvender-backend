"""Customers as the admin sees them: users and accounts (metadata only), blocking, account decisions.

The admin never sees email bodies, templates, datasets, API key secrets or password hashes.
Blocking a user and every account decision is audited with the admin, their IP and a reason.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import models as m
from app.errors import Conflict, NotFound
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.admin_auth import AdminAuth, admin_audit
from app.services.auth import send_password_reset, send_verification
from app.services.dns import DnsResolver
from app.services.domains import DomainService, DomainView
from app.services.health import AccountHealth, HealthService
from app.services.sending import suspend


def user_status(user: m.User) -> str:
    if user.deleted_at:
        return "deleted"
    if user.disabled_at:
        return "blocked"
    return "active" if user.email_verified_at else "unverified"


def account_status(account: m.Account) -> str:
    if account.status != "active":
        return "deleted"
    return "suspended" if account.sending_suspended_at else "active"


def mask_email(email: str) -> str:
    """`jo***@gmail.com` – enough to recognise a pattern, not to read a customer's list."""
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"


@dataclass
class UserRow:
    user: m.User
    accounts: int
    last_seen_at: datetime | None
    sent_30d: int

    @property
    def status(self) -> str:
        return user_status(self.user)


@dataclass
class UserDetail:
    user: m.User
    accounts: list[tuple[m.Account, str]]
    active_sessions: int
    last_seen_at: datetime | None


@dataclass
class AccountRow:
    health: AccountHealth
    owner_email: str | None
    members: int
    verified_domains: int
    sent_today: int


@dataclass
class AccountDetail:
    health: AccountHealth
    owner_email: str | None
    members: list[tuple[m.User, str]]
    domains: list[DomainView]
    senders: list[m.SenderIdentity]
    api_keys: list[m.ApiKey]
    recent_events: list[m.DeliveryEvent]
    suppressions: int
    audit: list[m.AuditEvent]


class NoDnsLookups:
    """Account pages show the DNS results already stored; they never look anything up."""

    def txt(self, name: str) -> list[str]:
        return []

    def mx(self, name: str) -> list[str]:
        return []


class AdminCustomersService:
    def __init__(self, uow: UnitOfWork, resolver: DnsResolver | None = None):
        self.uow = uow
        self.resolver = resolver or NoDnsLookups()

    # ---- Users ----

    def users_page(self, page: PageRequest, query: str | None, status: str | None) -> Page[UserRow]:
        result = self.uow.admin_stats.users_page(page, query=query, status=status,
                                                 since=utcnow() - timedelta(days=30))
        return Page(items=[UserRow(user, accounts, last_seen, sent) for user, accounts, last_seen, sent in result.items],
                    next_cursor=result.next_cursor)

    def _user(self, user_id: uuid.UUID) -> m.User:
        user = self.uow.users.get(user_id)
        if user is None:
            raise NotFound("User not found.")
        return user

    def _live_user(self, user_id: uuid.UUID) -> m.User:
        user = self._user(user_id)
        if user.deleted_at:
            raise Conflict("This person deleted their login.", code="user_deleted")
        return user

    def user_detail(self, auth: AdminAuth, user_id: uuid.UUID, *, record_view: bool = True) -> UserDetail:
        user = self._user(user_id)
        if record_view:
            admin_audit(self.uow, auth, "admin.viewed_user", target_type="user", target_id=user.id)
            self.uow.commit()
        return UserDetail(user=user, accounts=self.uow.admin_stats.user_accounts(user.id),
                          active_sessions=self.uow.admin_stats.count_active_sessions(user.id, utcnow()),
                          last_seen_at=self.uow.admin_stats.last_seen(user.id))

    def block_user(self, auth: AdminAuth, user_id: uuid.UUID, reason: str, suspend_accounts: bool) -> UserDetail:
        user = self._live_user(user_id)
        if user.disabled_at:
            raise Conflict("This user is already blocked.", code="already_blocked")
        now = utcnow()
        user.disabled_at = now
        self.uow.sessions.revoke_for_users([user.id], now)  # signed out everywhere, right now
        suspended: list[str] = []
        if suspend_accounts:
            for account in self.uow.admin_stats.owned_accounts(user.id):
                if account.status != "active":
                    continue
                if not account.sending_suspended_at:
                    suspend(self.uow, account, auth.actor, "owner_blocked", f"Owner blocked: {reason}")
                    suspended.append(str(account.id))
                self.uow.api_keys.revoke_all(account.id, now)
        admin_audit(self.uow, auth, "user.blocked", target_type="user",
                    target_id=user.id, reason=reason, suspended_accounts=suspended)
        self.uow.commit()
        return self.user_detail(auth, user.id, record_view=False)

    def unblock_user(self, auth: AdminAuth, user_id: uuid.UUID, reason: str) -> UserDetail:
        user = self._live_user(user_id)
        if not user.disabled_at:
            raise Conflict("This user isn't blocked.", code="not_blocked")
        user.disabled_at = None
        # Sending stays suspended until an admin reinstates each account – a deliberate second step.
        admin_audit(self.uow, auth, "user.unblocked", target_type="user",
                    target_id=user.id, reason=reason)
        self.uow.commit()
        return self.user_detail(auth, user.id, record_view=False)

    def sign_out_everywhere(self, auth: AdminAuth, user_id: uuid.UUID, reason: str) -> UserDetail:
        """Ends every session (e.g. a lost laptop) without blocking the person."""
        user = self._live_user(user_id)
        revoked = self.uow.sessions.revoke_for_users([user.id], utcnow())
        admin_audit(self.uow, auth, "user.signed_out_everywhere", target_type="user", target_id=user.id,
                    reason=reason, sessions=revoked)
        self.uow.commit()
        return self.user_detail(auth, user.id, record_view=False)

    def send_password_reset(self, auth: AdminAuth, user_id: uuid.UUID) -> UserDetail:
        user = self._live_user(user_id)
        if user.disabled_at:
            raise Conflict("This user is blocked.", code="user_blocked")
        if user.email_verified_at is None:
            raise Conflict("They haven't verified their email yet – resend the verification link instead.",
                           code="not_verified")
        send_password_reset(self.uow, user)
        admin_audit(self.uow, auth, "user.password_reset_sent", target_type="user", target_id=user.id)
        self.uow.commit()
        return self.user_detail(auth, user.id, record_view=False)

    def resend_verification(self, auth: AdminAuth, user_id: uuid.UUID) -> UserDetail:
        user = self._live_user(user_id)
        if user.email_verified_at:
            raise Conflict("This user is already verified.", code="already_verified")
        if user.disabled_at:
            raise Conflict("This user is blocked.", code="user_blocked")
        send_verification(self.uow, user)
        admin_audit(self.uow, auth, "user.verification_resent", target_type="user", target_id=user.id)
        self.uow.commit()
        return self.user_detail(auth, user.id, record_view=False)

    # ---- Accounts ----

    def accounts_page(self, page: PageRequest, query: str | None, status: str | None) -> Page[AccountRow]:
        health = HealthService(self.uow)
        # Same window as the daily sending limit: the last 24 hours, not the calendar day.
        result = self.uow.admin_stats.accounts_page(page, query=query, status=status,
                                                    since=utcnow() - timedelta(days=1))
        return Page(
            items=[AccountRow(AccountHealth(account, health.metrics(account), None), owner, members, domains, sent)
                   for account, owner, members, domains, sent in result.items],
            next_cursor=result.next_cursor,
        )

    def account_detail(self, auth: AdminAuth, account_id: uuid.UUID, *, record_view: bool = True) -> AccountDetail:
        health = HealthService(self.uow).get(account_id)
        if record_view:
            admin_audit(self.uow, auth, "admin.viewed_account", target_type="account",
                        target_id=account_id)
            self.uow.commit()
        domains = DomainService(self.uow, self.resolver)
        return AccountDetail(
            health=health,
            owner_email=self.uow.admin_stats.owner_emails([account_id]).get(account_id),
            members=self.uow.admin_stats.account_members(account_id),
            domains=[domains.view(d) for d in self.uow.domains.list_all_for_account(account_id)],
            senders=self.uow.sender_identities.list_all_for_account(account_id),
            api_keys=self.uow.api_keys.list_page(account_id, PageRequest(limit=100)).items,
            recent_events=self.uow.admin_stats.recent_events(account_id, 20),
            suppressions=self.uow.admin_stats.count_suppressions(account_id),
            audit=self.uow.audit.list_page(account_id, PageRequest(limit=25)).items,
        )

    def decide(self, auth: AdminAuth, account_id: uuid.UUID, decision: str, reason: str,
               hourly: int | None = None, daily: int | None = None) -> AccountDetail:
        """suspend | reinstate | limits | review – the operator decisions of HealthService, as this admin."""
        health = HealthService(self.uow)
        actor = auth.actor
        if decision == "suspend":
            health.suspend(actor, account_id, reason)
        elif decision == "reinstate":
            health.reinstate(actor, account_id, reason)
        elif decision == "limits":
            assert hourly is not None and daily is not None
            health.set_limits(actor, account_id, hourly, daily, reason)
        else:
            health.review(actor, account_id, reason)
        admin_audit(self.uow, auth, f"admin.account_{decision}", target_type="account",
                    target_id=account_id, reason=reason)
        self.uow.commit()
        return self.account_detail(auth, account_id, record_view=False)

    def audit_page(self, page: PageRequest, admin_id: uuid.UUID | None, action: str | None) -> Page[m.AuditEvent]:
        return self.uow.admin_stats.operator_audit_page(page, actor_id=admin_id, action=action)

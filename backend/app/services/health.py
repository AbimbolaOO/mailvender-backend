"""Account health: rolling bounce/complaint rates, automatic suspension, operator decisions.

Rates use recipient-level delivery events over the last `HEALTH_WINDOW_DAYS`
(default 7) divided by recipients accepted by the MTA in the same window:

* bounce rate    = hard bounces / accepted      (suspend above HEALTH_MAX_BOUNCE_RATE, default 5%)
* complaint rate = complaints / accepted        (suspend above HEALTH_MAX_COMPLAINT_RATE, default 0.1%)

Rates are only acted on once the window holds at least HEALTH_MIN_VOLUME
(default 100) accepted recipients, so one bounce from a new account doesn't
suspend it.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from app import models as m
from app.config import get_settings
from app.errors import ApiError, Forbidden, NotFound, ValidationFailed, field_error
from app.pagination import Page, PageRequest, decode_cursor
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.common import Actor, audit
from app.services.sending import suspend

DECISION_ACTIONS = ("account.suspended", "account.reinstated", "account.limits_changed", "account.reviewed")


@dataclass
class HealthMetrics:
    window_days: int
    accepted: int
    hard_bounces: int
    soft_bounces: int
    complaints: int
    bounce_rate: float
    complaint_rate: float
    sent_last_hour: int
    sent_last_day: int


@dataclass
class AccountHealth:
    account: m.Account
    metrics: HealthMetrics
    latest_decision: m.AuditEvent | None


class HealthService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow
        self.settings = get_settings()

    def metrics(self, account: m.Account) -> HealthMetrics:
        now = utcnow()
        since = now - timedelta(days=self.settings.health_window_days)
        accepted = self.uow.messages.count_accepted_since(account.id, since)
        events = self.uow.delivery.count_events_since(account.id, since)
        return HealthMetrics(
            window_days=self.settings.health_window_days,
            accepted=accepted,
            hard_bounces=events["hard_bounces"],
            soft_bounces=events["soft_bounces"],
            complaints=events["complaints"],
            bounce_rate=events["hard_bounces"] / accepted if accepted else 0.0,
            complaint_rate=events["complaints"] / accepted if accepted else 0.0,
            sent_last_hour=self.uow.messages.count_recipients_since(account.id, now - timedelta(hours=1)),
            sent_last_day=self.uow.messages.count_recipients_since(account.id, now - timedelta(days=1)),
        )

    def evaluate(self, account: m.Account) -> bool:
        """Suspends `account` if its rates cross the thresholds. Returns True when it did."""
        if account.status != "active" or account.sending_suspended_at:
            return False
        metrics = self.metrics(account)
        if metrics.accepted < self.settings.health_min_volume:
            return False
        if metrics.complaint_rate > self.settings.health_max_complaint_rate:
            suspend(self.uow, account, Actor.system(), "complaint_rate",
                    f"Complaint rate {metrics.complaint_rate:.2%} over {metrics.window_days} days "
                    f"(limit {self.settings.health_max_complaint_rate:.2%})")
            return True
        if metrics.bounce_rate > self.settings.health_max_bounce_rate:
            suspend(self.uow, account, Actor.system(), "bounce_rate",
                    f"Bounce rate {metrics.bounce_rate:.2%} over {metrics.window_days} days "
                    f"(limit {self.settings.health_max_bounce_rate:.2%})")
            return True
        return False

    def evaluate_all(self) -> int:
        suspended = 0
        page = PageRequest(limit=100)
        while True:
            result = self.uow.accounts.list_page(page)
            for account in result.items:
                suspended += self.evaluate(account)
            self.uow.commit()
            if not result.next_cursor:
                return suspended
            page = PageRequest(limit=100, cursor=decode_cursor(result.next_cursor))

    # ---- Operator API ----

    @staticmethod
    def require_operator(user: m.User) -> None:
        if not user.is_operator:
            raise Forbidden("Operators only.", code="operator_required")

    def list_page(self, page: PageRequest) -> Page[AccountHealth]:
        result = self.uow.accounts.list_page(page)
        return Page(
            items=[
                AccountHealth(account, self.metrics(account),
                              self.uow.audit.latest_for_account(account.id, DECISION_ACTIONS))
                for account in result.items
            ],
            next_cursor=result.next_cursor,
        )

    def get(self, account_id: uuid.UUID) -> AccountHealth:
        account = self.uow.accounts.get(account_id)
        if account is None:
            raise NotFound("Account not found.")
        return AccountHealth(account, self.metrics(account),
                             self.uow.audit.latest_for_account(account.id, DECISION_ACTIONS))

    def _account(self, account_id: uuid.UUID) -> m.Account:
        account = self.uow.accounts.get(account_id, for_update=True)
        if account is None:
            raise NotFound("Account not found.")
        return account

    def suspend(self, operator: m.User, account_id: uuid.UUID, reason: str) -> AccountHealth:
        account = self._account(account_id)
        if account.sending_suspended_at:
            raise ApiError("Already suspended.", code="already_suspended", status_code=409)
        suspend(self.uow, account, Actor("operator", operator.id), "manual", reason)
        self.uow.commit()
        return self.get(account_id)

    def reinstate(self, operator: m.User, account_id: uuid.UUID, reason: str) -> AccountHealth:
        account = self._account(account_id)
        if not account.sending_suspended_at:
            raise ApiError("This account isn't suspended.", code="not_suspended", status_code=409)
        account.sending_suspended_at = None
        account.suspension_reason = None
        audit(self.uow, account.id, Actor("operator", operator.id), "account.reinstated", target_type="account",
              target_id=account.id, reason=reason)
        self.uow.commit()
        return self.get(account_id)

    def set_limits(self, operator: m.User, account_id: uuid.UUID, hourly: int, daily: int, reason: str) -> AccountHealth:
        if hourly < 0 or daily < 0 or hourly > daily:
            raise ValidationFailed([field_error("hourly_recipient_limit",
                                                "Limits must be positive and hourly ≤ daily.", "invalid")])
        account = self._account(account_id)
        previous = {"hourly": account.hourly_recipient_limit, "daily": account.daily_recipient_limit}
        account.hourly_recipient_limit, account.daily_recipient_limit = hourly, daily
        audit(self.uow, account.id, Actor("operator", operator.id), "account.limits_changed", target_type="account",
              target_id=account.id, reason=reason, previous=previous, hourly=hourly, daily=daily)
        self.uow.commit()
        return self.get(account_id)

    def review(self, operator: m.User, account_id: uuid.UUID, notes: str) -> AccountHealth:
        account = self._account(account_id)
        metrics = self.metrics(account)
        audit(self.uow, account.id, Actor("operator", operator.id), "account.reviewed", target_type="account",
              target_id=account.id, reason=notes, bounce_rate=metrics.bounce_rate,
              complaint_rate=metrics.complaint_rate, accepted=metrics.accepted)
        self.uow.commit()
        return self.get(account_id)

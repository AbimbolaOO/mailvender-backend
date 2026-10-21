"""SQLAlchemy implementations of the repository interfaces."""

import uuid
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any, TypeVar

from sqlalchemy import Select, Text, and_, cast, delete, func, or_, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models as m
from app.pagination import Page, PageRequest, encode_cursor
from app.repositories.interfaces import DuplicateError

E = TypeVar("E")


def _paginate(session: Session, stmt: Any, model: Any, page: PageRequest, *, rows: bool = False) -> Page[Any]:
    """Keyset pagination on (created_at DESC, id DESC) of `model`."""
    if page.cursor:
        created_at, id_ = page.cursor
        stmt = stmt.where(tuple_(model.created_at, model.id) < tuple_(created_at, id_))
    stmt = stmt.order_by(model.created_at.desc(), model.id.desc()).limit(page.limit + 1)
    result = session.execute(stmt)
    items = list(result.all() if rows else result.scalars().all())
    next_cursor = None
    if len(items) > page.limit:
        items = items[: page.limit]
        last = items[-1][0] if rows else items[-1]
        next_cursor = encode_cursor(last.created_at, last.id)
    return Page(items=[tuple(item) for item in items] if rows else items, next_cursor=next_cursor)


def _rows(result: Any) -> int:
    return int(result.rowcount or 0)


class _Repo:
    model: Any = None

    def __init__(self, session: Session):
        self.s = session

    def add(self, entity: E) -> E:
        self.s.add(entity)
        self.s.flush()
        return entity

    def _get(self, id_: uuid.UUID, account_id: uuid.UUID | None = None, for_update: bool = False) -> Any:
        stmt = select(self.model).where(self.model.id == id_)
        if account_id is not None:
            stmt = stmt.where(self.model.account_id == account_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.s.execute(stmt).scalar_one_or_none()


class SqlUsers(_Repo):
    model = m.User

    def get(self, user_id: uuid.UUID) -> m.User | None:
        return self._get(user_id)

    def get_by_email(self, email: str) -> m.User | None:
        return self.s.execute(select(m.User).where(m.User.email == email)).scalar_one_or_none()

    def mark_active(self, user_id: uuid.UUID, day: date) -> None:
        self.s.execute(insert(m.UserActivityDay).values(user_id=user_id, day=day).on_conflict_do_nothing())


class SqlSessions(_Repo):
    model = m.Session

    def get_by_token_hash(self, token_hash: str) -> m.Session | None:
        return self.s.execute(select(m.Session).where(m.Session.token_hash == token_hash)).scalar_one_or_none()

    def revoke_for_users(self, user_ids: Iterable[uuid.UUID], now: datetime) -> int:
        ids = list(user_ids)
        if not ids:
            return 0
        result = self.s.execute(
            update(m.Session)
            .where(m.Session.user_id.in_(ids), m.Session.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return _rows(result)

    def list_active_for_user(self, user_id: uuid.UUID, now: datetime) -> list[m.Session]:
        stmt = (select(m.Session)
                .where(m.Session.user_id == user_id, m.Session.revoked_at.is_(None), m.Session.expires_at > now)
                .order_by(m.Session.created_at))
        return list(self.s.execute(stmt).scalars())

    def delete_expired_before(self, before: datetime) -> int:
        result = self.s.execute(
            delete(m.Session).where(or_(m.Session.expires_at < before, m.Session.revoked_at < before))
        )
        return _rows(result)


class SqlEmailTokens(_Repo):
    model = m.EmailToken

    def get_by_hash(self, token_hash: str, purpose: str, *, for_update: bool = False) -> m.EmailToken | None:
        stmt = select(m.EmailToken).where(m.EmailToken.token_hash == token_hash, m.EmailToken.purpose == purpose)
        if for_update:
            stmt = stmt.with_for_update()
        return self.s.execute(stmt).scalar_one_or_none()

    def invalidate_for_user(self, user_id: uuid.UUID, purpose: str, now: datetime) -> None:
        self.s.execute(
            update(m.EmailToken)
            .where(m.EmailToken.user_id == user_id, m.EmailToken.purpose == purpose, m.EmailToken.used_at.is_(None))
            .values(used_at=now)
        )

    def delete_expired_before(self, before: datetime) -> int:
        return _rows(self.s.execute(delete(m.EmailToken).where(m.EmailToken.expires_at < before)))

    def delete_for_user(self, user_id: uuid.UUID) -> None:
        self.s.execute(delete(m.EmailToken).where(m.EmailToken.user_id == user_id))


class SqlAccounts(_Repo):
    model = m.Account

    def get(self, account_id: uuid.UUID, *, for_update: bool = False) -> m.Account | None:
        return self._get(account_id, for_update=for_update)

    def list_for_user(self, user_id: uuid.UUID) -> list[tuple[m.Account, str]]:
        rows = self.s.execute(
            select(m.Account, m.Membership.role)
            .join(m.Membership, m.Membership.account_id == m.Account.id)
            .where(m.Membership.user_id == user_id, m.Account.status == "active")
            .order_by(m.Account.kind, m.Account.created_at)  # personal first
        ).all()
        return [(account, role) for account, role in rows]

    def list_page(self, page: PageRequest) -> Page[m.Account]:
        return _paginate(self.s, select(m.Account), m.Account, page)

    def deleted_before(self, before: datetime) -> list[m.Account]:
        return list(
            self.s.execute(
                select(m.Account).where(m.Account.status == "deleted", m.Account.deletion_requested_at < before,
                                        m.Account.deleted_at.is_(None))
            ).scalars()
        )

    def list_memberships_for_user(self, user_id: uuid.UUID) -> list[tuple[m.Account, m.Membership]]:
        """Every account the user belongs to, including deleted ones not yet purged."""
        rows = self.s.execute(
            select(m.Account, m.Membership)
            .join(m.Membership, m.Membership.account_id == m.Account.id)
            .where(m.Membership.user_id == user_id, m.Account.deleted_at.is_(None))
            .order_by(m.Account.kind, m.Account.created_at)
        ).all()
        return [(account, membership) for account, membership in rows]


class SqlMemberships(_Repo):
    model = m.Membership

    def get(self, account_id: uuid.UUID, user_id: uuid.UUID, *, for_update: bool = False) -> m.Membership | None:
        stmt = select(m.Membership).where(m.Membership.account_id == account_id, m.Membership.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.s.execute(stmt).scalar_one_or_none()

    def list_for_account(self, account_id: uuid.UUID, page: PageRequest) -> Page[tuple[m.Membership, m.User]]:
        stmt = (
            select(m.Membership, m.User)
            .join(m.User, m.User.id == m.Membership.user_id)
            .where(m.Membership.account_id == account_id)
        )
        return _paginate(self.s, stmt, m.Membership, page, rows=True)

    def list_user_ids(self, account_id: uuid.UUID) -> list[uuid.UUID]:
        return list(self.s.execute(select(m.Membership.user_id).where(m.Membership.account_id == account_id)).scalars())

    def count_owners(self, account_id: uuid.UUID) -> int:
        return self.s.execute(
            select(func.count()).where(m.Membership.account_id == account_id, m.Membership.role == "owner")
        ).scalar_one()

    def delete(self, membership: m.Membership) -> None:
        self.s.delete(membership)
        self.s.flush()

    def delete_all_for_account(self, account_id: uuid.UUID) -> None:
        self.s.execute(delete(m.Membership).where(m.Membership.account_id == account_id))

    def delete_all_for_user(self, user_id: uuid.UUID) -> None:
        self.s.execute(delete(m.Membership).where(m.Membership.user_id == user_id))


class SqlInvitations(_Repo):
    model = m.Invitation

    def get(self, invitation_id: uuid.UUID, account_id: uuid.UUID) -> m.Invitation | None:
        return self._get(invitation_id, account_id)

    def get_by_hash(self, token_hash: str, *, for_update: bool = False) -> m.Invitation | None:
        stmt = select(m.Invitation).where(m.Invitation.token_hash == token_hash)
        if for_update:
            stmt = stmt.with_for_update()
        return self.s.execute(stmt).scalar_one_or_none()

    def list_pending(self, account_id: uuid.UUID, now: datetime, page: PageRequest) -> Page[m.Invitation]:
        stmt = select(m.Invitation).where(
            m.Invitation.account_id == account_id,
            m.Invitation.accepted_at.is_(None),
            m.Invitation.revoked_at.is_(None),
            m.Invitation.expires_at > now,
        )
        return _paginate(self.s, stmt, m.Invitation, page)

    def list_for_email(self, email: str) -> list[m.Invitation]:
        stmt = select(m.Invitation).where(m.Invitation.email == email).order_by(m.Invitation.created_at)
        return list(self.s.execute(stmt).scalars())

    def revoke_pending_for_email(self, email: str, now: datetime) -> int:
        return _rows(self.s.execute(
            update(m.Invitation)
            .where(m.Invitation.email == email, m.Invitation.accepted_at.is_(None), m.Invitation.revoked_at.is_(None))
            .values(revoked_at=now)
        ))


class SqlApiKeys(_Repo):
    model = m.ApiKey

    def get(self, key_id: uuid.UUID, account_id: uuid.UUID) -> m.ApiKey | None:
        return self._get(key_id, account_id)

    def get_by_hash(self, key_hash: str) -> m.ApiKey | None:
        return self.s.execute(select(m.ApiKey).where(m.ApiKey.key_hash == key_hash)).scalar_one_or_none()

    def list_page(self, account_id: uuid.UUID, page: PageRequest) -> Page[m.ApiKey]:
        return _paginate(self.s, select(m.ApiKey).where(m.ApiKey.account_id == account_id), m.ApiKey, page)

    def revoke_all(self, account_id: uuid.UUID, now: datetime) -> int:
        return _rows(self.s.execute(
            update(m.ApiKey)
            .where(m.ApiKey.account_id == account_id, m.ApiKey.revoked_at.is_(None))
            .values(revoked_at=now)
        ))


class SqlDomains(_Repo):
    model = m.Domain

    def get(self, domain_id: uuid.UUID, account_id: uuid.UUID) -> m.Domain | None:
        return self._get(domain_id, account_id)

    def get_live_by_name(self, name: str) -> m.Domain | None:
        return self.s.execute(
            select(m.Domain).where(m.Domain.name == name, m.Domain.disabled_at.is_(None))
        ).scalar_one_or_none()

    def list_page(self, account_id: uuid.UUID, page: PageRequest) -> Page[m.Domain]:
        return _paginate(self.s, select(m.Domain).where(m.Domain.account_id == account_id), m.Domain, page)

    def list_all_for_account(self, account_id: uuid.UUID) -> list[m.Domain]:
        return list(self.s.execute(select(m.Domain).where(m.Domain.account_id == account_id)).scalars())

    def list_due_for_recheck(self, checked_before: datetime, limit: int) -> list[m.Domain]:
        return list(
            self.s.execute(
                select(m.Domain)
                .where(
                    m.Domain.status == "verified",
                    or_(m.Domain.last_checked_at.is_(None), m.Domain.last_checked_at < checked_before),
                )
                .order_by(m.Domain.last_checked_at.nulls_first())
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).scalars()
        )


class SqlDkimKeys(_Repo):
    model = m.DkimKey

    def list_for_domain(self, domain_id: uuid.UUID) -> list[m.DkimKey]:
        return list(
            self.s.execute(
                select(m.DkimKey).where(m.DkimKey.domain_id == domain_id).order_by(m.DkimKey.created_at)
            ).scalars()
        )

    def list_signing(self) -> list[tuple[m.DkimKey, m.Domain]]:
        # Only verified domains: an unverified one may not belong to the account that added it.
        rows = self.s.execute(
            select(m.DkimKey, m.Domain)
            .join(m.Domain, m.Domain.id == m.DkimKey.domain_id)
            .where(m.DkimKey.status == "active", m.Domain.status == "verified", m.Domain.disabled_at.is_(None))
        ).all()
        return [(key, domain) for key, domain in rows]

    def delete_retired_before(self, before: datetime) -> int:
        return _rows(self.s.execute(
            delete(m.DkimKey).where(m.DkimKey.status == "retired", m.DkimKey.retired_at < before)
        ))


class SqlPlatformDkimKeys(_Repo):
    model = m.PlatformDkimKey

    def list_for_domain(self, domain: str) -> list[m.PlatformDkimKey]:
        return list(
            self.s.execute(
                select(m.PlatformDkimKey).where(m.PlatformDkimKey.domain == domain)
                .order_by(m.PlatformDkimKey.created_at)
            ).scalars()
        )

    def delete_retired_before(self, before: datetime) -> int:
        return _rows(self.s.execute(
            delete(m.PlatformDkimKey).where(m.PlatformDkimKey.status == "retired",
                                            m.PlatformDkimKey.retired_at < before)
        ))


class SqlSenderIdentities(_Repo):
    model = m.SenderIdentity

    def get(self, identity_id: uuid.UUID, account_id: uuid.UUID) -> m.SenderIdentity | None:
        return self._get(identity_id, account_id)

    def get_by_email(self, account_id: uuid.UUID, email: str) -> m.SenderIdentity | None:
        return self.s.execute(
            select(m.SenderIdentity).where(m.SenderIdentity.account_id == account_id, m.SenderIdentity.email == email)
        ).scalar_one_or_none()

    def list_page(self, account_id: uuid.UUID, page: PageRequest) -> Page[m.SenderIdentity]:
        stmt = select(m.SenderIdentity).where(m.SenderIdentity.account_id == account_id)
        return _paginate(self.s, stmt, m.SenderIdentity, page)

    def list_for_domain(self, domain_id: uuid.UUID) -> list[m.SenderIdentity]:
        return list(self.s.execute(select(m.SenderIdentity).where(m.SenderIdentity.domain_id == domain_id)).scalars())

    def list_all_for_account(self, account_id: uuid.UUID) -> list[m.SenderIdentity]:
        return list(
            self.s.execute(select(m.SenderIdentity).where(m.SenderIdentity.account_id == account_id)).scalars()
        )


class SqlAssets(_Repo):
    model = m.Asset

    def get(self, asset_id: uuid.UUID, account_id: uuid.UUID) -> m.Asset | None:
        return self._get(asset_id, account_id)

    def list_ready(self, account_id: uuid.UUID, page: PageRequest) -> Page[m.Asset]:
        stmt = select(m.Asset).where(m.Asset.account_id == account_id, m.Asset.status == "ready")
        return _paginate(self.s, stmt, m.Asset, page)

    def list_all_for_account(self, account_id: uuid.UUID) -> list[m.Asset]:
        return list(self.s.execute(select(m.Asset).where(m.Asset.account_id == account_id)).scalars())

    def list_stale_pending(self, before: datetime, limit: int) -> list[m.Asset]:
        return list(
            self.s.execute(
                select(m.Asset).where(m.Asset.status == "pending", m.Asset.upload_expires_at < before).limit(limit)
            ).scalars()
        )


class SqlMessages(_Repo):
    model = m.Message

    def add(self, entity: E) -> E:
        try:
            with self.s.begin_nested():
                self.s.add(entity)
                self.s.flush()
        except IntegrityError as error:
            raise DuplicateError(str(error.orig)) from error
        return entity

    def get(self, message_id: uuid.UUID, account_id: uuid.UUID) -> m.Message | None:
        return self._get(message_id, account_id)

    def get_message_by_id(self, message_id: uuid.UUID) -> m.Message | None:
        return self._get(message_id)

    def get_by_idempotency_key(self, account_id: uuid.UUID, key: str) -> m.Message | None:
        return self.s.execute(
            select(m.Message).where(m.Message.account_id == account_id, m.Message.idempotency_key == key)
        ).scalar_one_or_none()

    def list_page(self, account_id: uuid.UUID, page: PageRequest) -> Page[m.Message]:
        return _paginate(self.s, select(m.Message).where(m.Message.account_id == account_id), m.Message, page)

    def add_recipient(self, recipient: m.MessageRecipient) -> m.MessageRecipient:
        return self.add(recipient)

    def get_recipient(self, recipient_id: uuid.UUID, *, for_update: bool = False) -> m.MessageRecipient | None:
        stmt = select(m.MessageRecipient).where(m.MessageRecipient.id == recipient_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.s.execute(stmt).scalar_one_or_none()

    def list_recipients(self, message_id: uuid.UUID) -> list[m.MessageRecipient]:
        return list(
            self.s.execute(
                select(m.MessageRecipient)
                .where(m.MessageRecipient.message_id == message_id)
                .order_by(m.MessageRecipient.created_at, m.MessageRecipient.email)
            ).scalars()
        )

    def count_recipients_since(self, account_id: uuid.UUID, since: datetime) -> int:
        return self.s.execute(
            select(func.count()).where(
                m.MessageRecipient.account_id == account_id, m.MessageRecipient.created_at >= since
            )
        ).scalar_one()

    def count_accepted_since(self, account_id: uuid.UUID, since: datetime) -> int:
        return self.s.execute(
            select(func.count()).where(
                m.MessageRecipient.account_id == account_id,
                m.MessageRecipient.accepted_at >= since,
            )
        ).scalar_one()

    def fail_pending_for_account(self, account_id: uuid.UUID, reason: str, now: datetime) -> int:
        return _rows(self.s.execute(
            update(m.MessageRecipient)
            .where(m.MessageRecipient.account_id == account_id, m.MessageRecipient.status == "queued")
            .values(status="failed", failed_at=now, updated_at=now, last_error=reason)
        ))

    def list_for_export(self, account_id: uuid.UUID, limit: int) -> list[tuple[m.Message, list[m.MessageRecipient]]]:
        messages = list(
            self.s.execute(
                select(m.Message).where(m.Message.account_id == account_id).order_by(m.Message.created_at).limit(limit)
            ).scalars()
        )
        return [(message, self.list_recipients(message.id)) for message in messages]

    def purge_content_before(self, before: datetime, now: datetime) -> int:
        return _rows(self.s.execute(
            update(m.Message)
            .where(m.Message.created_at < before, m.Message.content_purged_at.is_(None))
            .values(html=None, text_body=None, content_purged_at=now)
        ))

    def delete_before(self, before: datetime) -> int:
        old = select(m.Message.id).where(m.Message.created_at < before).scalar_subquery()
        old_recipients = select(m.MessageRecipient.id).where(m.MessageRecipient.message_id.in_(old))
        self.s.execute(delete(m.DeliveryAttempt).where(m.DeliveryAttempt.recipient_id.in_(old_recipients)))
        self.s.execute(
            update(m.DeliveryEvent).where(m.DeliveryEvent.recipient_id.in_(old_recipients)).values(recipient_id=None)
        )
        self.s.execute(
            update(m.Suppression).where(m.Suppression.recipient_id.in_(old_recipients)).values(recipient_id=None)
        )
        self.s.execute(delete(m.MessageRecipient).where(m.MessageRecipient.message_id.in_(old)))
        return _rows(self.s.execute(delete(m.Message).where(m.Message.created_at < before)))


class SqlOutbox(_Repo):
    model = m.OutboxEvent

    def claim(self, now: datetime, limit: int) -> list[m.OutboxEvent]:
        return list(
            self.s.execute(
                select(m.OutboxEvent)
                .where(m.OutboxEvent.status == "pending", m.OutboxEvent.available_at <= now)
                .order_by(m.OutboxEvent.available_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).scalars()
        )

    def cancel_for_account(self, account_id: uuid.UUID) -> int:
        return _rows(self.s.execute(
            update(m.OutboxEvent)
            .where(
                m.OutboxEvent.account_id == account_id,
                m.OutboxEvent.status == "pending",
                m.OutboxEvent.kind == "deliver_recipient",
            )
            .values(status="cancelled")
        ))

    def delete_processed_before(self, before: datetime) -> int:
        return _rows(self.s.execute(
            delete(m.OutboxEvent).where(
                m.OutboxEvent.status.in_(["done", "cancelled", "dead"]), m.OutboxEvent.created_at < before
            )
        ))

    def count_by_status(self) -> dict[str, int]:
        rows = self.s.execute(select(m.OutboxEvent.status, func.count()).group_by(m.OutboxEvent.status)).all()
        return {status: count for status, count in rows}


class SqlDelivery(_Repo):
    def add_attempt(self, attempt: m.DeliveryAttempt) -> m.DeliveryAttempt:
        return self.add(attempt)

    def add_event(self, event: m.DeliveryEvent) -> m.DeliveryEvent:
        return self.add(event)

    def count_soft_bounces(self, account_id: uuid.UUID, email: str, since: datetime) -> int:
        return self.s.execute(
            select(func.count()).where(
                m.DeliveryEvent.account_id == account_id,
                m.DeliveryEvent.email == email,
                m.DeliveryEvent.type == "bounce",
                m.DeliveryEvent.classification == "soft",
                m.DeliveryEvent.created_at >= since,
            )
        ).scalar_one()

    def count_events_since(self, account_id: uuid.UUID, since: datetime) -> dict[str, int]:
        rows = self.s.execute(
            select(m.DeliveryEvent.type, m.DeliveryEvent.classification, func.count())
            .where(m.DeliveryEvent.account_id == account_id, m.DeliveryEvent.created_at >= since)
            .group_by(m.DeliveryEvent.type, m.DeliveryEvent.classification)
        ).all()
        counts = {"hard_bounces": 0, "soft_bounces": 0, "complaints": 0}
        for type_, classification, count in rows:
            if type_ == "complaint":
                counts["complaints"] += count
            elif classification == "hard":
                counts["hard_bounces"] += count
            else:
                counts["soft_bounces"] += count
        return counts

    def delete_before(self, before: datetime) -> int:
        self.s.execute(delete(m.DeliveryAttempt).where(m.DeliveryAttempt.created_at < before))
        return _rows(self.s.execute(delete(m.DeliveryEvent).where(m.DeliveryEvent.created_at < before)))


class SqlSuppressions(_Repo):
    model = m.Suppression

    def get(self, account_id: uuid.UUID, email: str) -> m.Suppression | None:
        return self.s.execute(
            select(m.Suppression).where(m.Suppression.account_id == account_id, m.Suppression.email == email)
        ).scalar_one_or_none()

    def add_if_absent(self, suppression: m.Suppression) -> tuple[m.Suppression, bool]:
        existing = self.get(suppression.account_id, suppression.email)
        if existing:
            return existing, False
        try:
            with self.s.begin_nested():
                self.s.add(suppression)
                self.s.flush()
        except IntegrityError:
            # Recorded concurrently – still idempotent.
            existing = self.get(suppression.account_id, suppression.email)
            assert existing is not None
            return existing, False
        return suppression, True

    def suppressed_among(self, account_id: uuid.UUID, emails: Sequence[str]) -> set[str]:
        if not emails:
            return set()
        return set(
            self.s.execute(
                select(m.Suppression.email).where(
                    m.Suppression.account_id == account_id, m.Suppression.email.in_(list(emails))
                )
            ).scalars()
        )

    def list_page(self, account_id: uuid.UUID, page: PageRequest) -> Page[m.Suppression]:
        stmt = select(m.Suppression).where(m.Suppression.account_id == account_id)
        return _paginate(self.s, stmt, m.Suppression, page)

    def list_all_for_account(self, account_id: uuid.UUID) -> list[m.Suppression]:
        return list(self.s.execute(select(m.Suppression).where(m.Suppression.account_id == account_id)).scalars())

    def delete(self, suppression: m.Suppression) -> None:
        self.s.delete(suppression)
        self.s.flush()


class SqlAudit(_Repo):
    model = m.AuditEvent

    def list_page(
        self,
        account_id: uuid.UUID,
        page: PageRequest,
        *,
        target_type: str | None = None,
        target_id: uuid.UUID | None = None,
    ) -> Page[m.AuditEvent]:
        stmt = select(m.AuditEvent).where(m.AuditEvent.account_id == account_id)
        if target_type:
            stmt = stmt.where(m.AuditEvent.target_type == target_type)
        if target_id:
            stmt = stmt.where(m.AuditEvent.target_id == target_id)
        return _paginate(self.s, stmt, m.AuditEvent, page)

    def list_by_user(self, user_id: uuid.UUID, limit: int) -> list[m.AuditEvent]:
        """What this person did, in any account (newest first)."""
        stmt = (select(m.AuditEvent)
                .where(m.AuditEvent.actor_type == "user", m.AuditEvent.actor_id == user_id)
                .order_by(m.AuditEvent.created_at.desc()).limit(limit))
        return list(self.s.execute(stmt).scalars())

    def latest_for_account(self, account_id: uuid.UUID, actions: Sequence[str]) -> m.AuditEvent | None:
        return self.s.execute(
            select(m.AuditEvent)
            .where(m.AuditEvent.account_id == account_id, m.AuditEvent.action.in_(list(actions)))
            .order_by(m.AuditEvent.created_at.desc(), m.AuditEvent.id.desc())
            .limit(1)
        ).scalar_one_or_none()


class SqlRateLimits(_Repo):
    def hit(self, key: str, window_start: datetime, amount: int = 1) -> int:
        stmt = (
            insert(m.RateLimitCounter)
            .values(key=key, window_start=window_start, count=amount)
            .on_conflict_do_update(
                index_elements=[m.RateLimitCounter.key, m.RateLimitCounter.window_start],
                set_={"count": m.RateLimitCounter.count + amount},
            )
            .returning(m.RateLimitCounter.count)
        )
        return self.s.execute(stmt).scalar_one()

    def delete_before(self, before: datetime) -> int:
        return _rows(self.s.execute(delete(m.RateLimitCounter).where(m.RateLimitCounter.window_start < before)))


class SqlSync(_Repo):
    model = m.SyncRecord

    def lock_account(self, account_id: uuid.UUID) -> None:
        # Serializes sync writes per account so `seq` commits in order.
        self.s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"sync:{account_id}"})

    def next_seq(self) -> int:
        return self.s.execute(text("SELECT nextval('sync_seq')")).scalar_one()

    def get(self, account_id: uuid.UUID, collection: str, record_id: uuid.UUID) -> m.SyncRecord | None:
        return self.s.execute(
            select(m.SyncRecord).where(
                m.SyncRecord.account_id == account_id,
                m.SyncRecord.collection == collection,
                m.SyncRecord.id == record_id,
            )
        ).scalar_one_or_none()

    def snapshot(
        self, account_id: uuid.UUID, collections: Sequence[str], after: tuple[str, uuid.UUID] | None, limit: int
    ) -> list[m.SyncRecord]:
        stmt = select(m.SyncRecord).where(
            m.SyncRecord.account_id == account_id,
            m.SyncRecord.deleted.is_(False),
            m.SyncRecord.collection.in_(list(collections)),
        )
        if after:
            stmt = stmt.where(tuple_(m.SyncRecord.collection, m.SyncRecord.id) > tuple_(after[0], after[1]))
        return list(self.s.execute(stmt.order_by(m.SyncRecord.collection, m.SyncRecord.id).limit(limit)).scalars())

    def changes(self, account_id: uuid.UUID, since: int, limit: int) -> list[m.SyncRecord]:
        return list(
            self.s.execute(
                select(m.SyncRecord)
                .where(m.SyncRecord.account_id == account_id, m.SyncRecord.seq > since)
                .order_by(m.SyncRecord.seq)
                .limit(limit)
            ).scalars()
        )

    def max_seq(self, account_id: uuid.UUID) -> int:
        return self.s.execute(
            select(func.coalesce(func.max(m.SyncRecord.seq), 0)).where(m.SyncRecord.account_id == account_id)
        ).scalar_one()

    def list_children(
        self, account_id: uuid.UUID, collection: str, parent_id: uuid.UUID, page: PageRequest
    ) -> Page[m.SyncRecord]:
        stmt = select(m.SyncRecord).where(
            and_(
                m.SyncRecord.account_id == account_id,
                m.SyncRecord.collection == collection,
                m.SyncRecord.parent_id == parent_id,
                m.SyncRecord.deleted.is_(False),
            )
        )
        return _paginate(self.s, stmt, m.SyncRecord, page)

    def list_all_for_account(self, account_id: uuid.UUID) -> list[m.SyncRecord]:
        return list(
            self.s.execute(
                select(m.SyncRecord)
                .where(m.SyncRecord.account_id == account_id, m.SyncRecord.deleted.is_(False))
                .order_by(m.SyncRecord.collection, m.SyncRecord.created_at)
            ).scalars()
        )

    def delete_all_for_account(self, account_id: uuid.UUID) -> int:
        self.s.execute(delete(m.SyncUpload).where(m.SyncUpload.account_id == account_id))
        return _rows(self.s.execute(delete(m.SyncRecord).where(m.SyncRecord.account_id == account_id)))

    def list_with_objects(self, collection: str, limit: int) -> list[m.SyncRecord]:
        return list(self.s.execute(
            select(m.SyncRecord).where(m.SyncRecord.collection == collection, m.SyncRecord.object_key.is_not(None))
            .limit(limit)
        ).scalars())

    def list_inline(self, collection: str, limit: int) -> list[m.SyncRecord]:
        return list(self.s.execute(
            select(m.SyncRecord).where(m.SyncRecord.collection == collection, m.SyncRecord.deleted.is_(False),
                                       m.SyncRecord.object_key.is_(None))
            .limit(limit)
        ).scalars())

    def add_upload(self, upload: m.SyncUpload) -> m.SyncUpload:
        self.s.add(upload)
        self.s.flush()
        return upload

    def get_upload(self, upload_id: uuid.UUID, account_id: uuid.UUID) -> m.SyncUpload | None:
        return self.s.execute(
            select(m.SyncUpload).where(m.SyncUpload.id == upload_id, m.SyncUpload.account_id == account_id)
        ).scalar_one_or_none()

    def delete_upload(self, upload: m.SyncUpload) -> None:
        self.s.delete(upload)

    def list_uploads_for_account(self, account_id: uuid.UUID) -> list[m.SyncUpload]:
        return list(self.s.execute(select(m.SyncUpload).where(m.SyncUpload.account_id == account_id)).scalars())

    def list_expired_uploads(self, before: datetime, limit: int) -> list[m.SyncUpload]:
        return list(self.s.execute(
            select(m.SyncUpload).where(m.SyncUpload.expires_at < before).order_by(m.SyncUpload.expires_at)
            .limit(limit)
        ).scalars())


class SqlExports(_Repo):
    model = m.AccountExport

    def get(self, export_id: uuid.UUID, account_id: uuid.UUID) -> m.AccountExport | None:
        return self._get(export_id, account_id)

    def get_by_id(self, export_id: uuid.UUID) -> m.AccountExport | None:
        return self._get(export_id)

    def list_page(self, account_id: uuid.UUID, page: PageRequest) -> Page[m.AccountExport]:
        stmt = select(m.AccountExport).where(m.AccountExport.account_id == account_id)
        return _paginate(self.s, stmt, m.AccountExport, page)

    def list_expired(self, now: datetime) -> list[m.AccountExport]:
        return list(
            self.s.execute(
                select(m.AccountExport).where(m.AccountExport.status == "ready", m.AccountExport.expires_at < now)
            ).scalars()
        )


class SqlJobs(_Repo):
    def try_lock(self, name: str) -> bool:
        return bool(
            self.s.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"job:{name}"}
            ).scalar_one()
        )

    def record(self, name: str, started_at: datetime, finished_at: datetime, error: str | None) -> None:
        self.s.execute(
            insert(m.JobRun)
            .values(name=name, last_started_at=started_at, last_finished_at=finished_at, last_error=error)
            .on_conflict_do_update(
                index_elements=[m.JobRun.name],
                set_={"last_started_at": started_at, "last_finished_at": finished_at, "last_error": error},
            )
        )

    def list_runs(self) -> list[m.JobRun]:
        return list(self.s.execute(select(m.JobRun).order_by(m.JobRun.name)).scalars())


# ---- Admin ----


class SqlAdmins(_Repo):
    model = m.AdminUser

    def get(self, admin_id: uuid.UUID, *, for_update: bool = False) -> m.AdminUser | None:
        return self._get(admin_id, for_update=for_update)

    def get_by_email(self, email: str, *, for_update: bool = False) -> m.AdminUser | None:
        stmt = select(m.AdminUser).where(m.AdminUser.email == email)
        if for_update:
            stmt = stmt.with_for_update()
        return self.s.execute(stmt).scalar_one_or_none()

    def list_all(self) -> list[m.AdminUser]:
        return list(self.s.execute(select(m.AdminUser).order_by(m.AdminUser.created_at)).scalars())

    def count_active(self) -> int:
        stmt = select(func.count()).select_from(m.AdminUser).where(m.AdminUser.disabled_at.is_(None))
        return int(self.s.execute(stmt).scalar_one())

    def lock(self) -> None:
        """Serialises admin creation (until commit), so two at once can't both pass the limit."""
        self.s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('admins', 0))"))

    def add_session(self, session: m.AdminSession) -> m.AdminSession:
        return self.add(session)

    def get_session_by_token_hash(self, token_hash: str) -> m.AdminSession | None:
        return self.s.execute(
            select(m.AdminSession).where(m.AdminSession.token_hash == token_hash)
        ).scalar_one_or_none()

    def revoke_sessions(self, admin_id: uuid.UUID, now: datetime, *, keep: uuid.UUID | None = None) -> int:
        stmt = update(m.AdminSession).where(m.AdminSession.admin_id == admin_id, m.AdminSession.revoked_at.is_(None))
        if keep is not None:
            stmt = stmt.where(m.AdminSession.id != keep)
        return _rows(self.s.execute(stmt.values(revoked_at=now)))

    def delete_sessions_before(self, before: datetime) -> int:
        return _rows(self.s.execute(
            delete(m.AdminSession).where(or_(m.AdminSession.expires_at < before, m.AdminSession.revoked_at < before))
        ))


class SqlAdminIssues(_Repo):
    def get_many(self, keys: Sequence[str]) -> dict[str, m.AdminIssueState]:
        if not keys:
            return {}
        rows = self.s.execute(select(m.AdminIssueState).where(m.AdminIssueState.key.in_(list(keys)))).scalars()
        return {row.key: row for row in rows}

    def upsert(self, key: str, status: str, note: str | None, admin_id: uuid.UUID, now: datetime) -> None:
        values = {"status": status, "note": note, "updated_by": admin_id, "updated_at": now}
        self.s.execute(
            insert(m.AdminIssueState).values(key=key, **values)
            .on_conflict_do_update(index_elements=[m.AdminIssueState.key], set_=values)
        )

    def delete(self, key: str) -> None:
        self.s.execute(delete(m.AdminIssueState).where(m.AdminIssueState.key == key))


class SqlInbound(_Repo):
    model = m.InboundMessage

    def add(self, entity: E) -> E:
        try:
            with self.s.begin_nested():
                self.s.add(entity)
                self.s.flush()
        except IntegrityError as error:
            raise DuplicateError(str(error.orig)) from error
        return entity

    def get(self, message_id: uuid.UUID) -> m.InboundMessage | None:
        return self._get(message_id)

    def get_by_message_id(self, mailbox: str, message_id_header: str) -> m.InboundMessage | None:
        return self.s.execute(
            select(m.InboundMessage).where(m.InboundMessage.mailbox == mailbox,
                                           m.InboundMessage.message_id_header == message_id_header)
        ).scalar_one_or_none()

    def list_page(self, mailbox: str, page: PageRequest, *, query: str | None, state: str) -> Page[m.InboundMessage]:
        stmt = select(m.InboundMessage).where(m.InboundMessage.mailbox == mailbox)
        if state == "archived":
            stmt = stmt.where(m.InboundMessage.archived_at.is_not(None))
        else:
            stmt = stmt.where(m.InboundMessage.archived_at.is_(None))
            if state == "unread":
                stmt = stmt.where(m.InboundMessage.read_at.is_(None))
        if query:
            like = f"%{_escape_like(query)}%"
            stmt = stmt.where(or_(m.InboundMessage.from_address.ilike(like, escape="\\"),
                                  m.InboundMessage.from_name.ilike(like, escape="\\"),
                                  m.InboundMessage.subject.ilike(like, escape="\\")))
        return _paginate(self.s, stmt, m.InboundMessage, page)

    def unread_counts(self) -> dict[str, int]:
        rows = self.s.execute(
            select(m.InboundMessage.mailbox, func.count())
            .where(m.InboundMessage.read_at.is_(None), m.InboundMessage.archived_at.is_(None))
            .group_by(m.InboundMessage.mailbox)
        ).all()
        return {mailbox: count for mailbox, count in rows}

    def list_received_before(self, before: datetime, limit: int) -> list[m.InboundMessage]:
        return list(self.s.execute(
            select(m.InboundMessage).where(m.InboundMessage.created_at < before)
            .order_by(m.InboundMessage.created_at).limit(limit)
        ).scalars())

    def delete(self, message: m.InboundMessage) -> None:
        # DMARC rows outlive their message (kept for a year), so they only lose the link.
        self.s.execute(update(m.DmarcReportRow).where(m.DmarcReportRow.inbound_message_id == message.id)
                       .values(inbound_message_id=None))
        self.s.delete(message)
        self.s.flush()

    def add_dmarc_rows(self, rows: Sequence[m.DmarcReportRow]) -> None:
        self.s.add_all(list(rows))
        self.s.flush()

    def dmarc_rows_for_message(self, message_id: uuid.UUID) -> list[m.DmarcReportRow]:
        return list(self.s.execute(
            select(m.DmarcReportRow).where(m.DmarcReportRow.inbound_message_id == message_id)
            .order_by(m.DmarcReportRow.count.desc())
        ).scalars())

    def _dmarc_totals(self, group: Any, since: datetime) -> list[dict[str, Any]]:
        row = m.DmarcReportRow
        passed = func.sum(row.count).filter(or_(row.dkim == "pass", row.spf == "pass"))
        rows = self.s.execute(
            select(
                group,
                func.sum(row.count),
                func.coalesce(passed, 0),
                func.coalesce(func.sum(row.count).filter(row.dkim == "pass"), 0),
                func.coalesce(func.sum(row.count).filter(row.spf == "pass"), 0),
                func.max(row.end_at),
            )
            .where(row.end_at >= since)
            .group_by(group)
            .order_by(func.sum(row.count).desc())
            .limit(200)
        ).all()
        return [
            {"key": key, "messages": int(total), "dmarc_pass": int(dmarc), "dkim_pass": int(dkim),
             "spf_pass": int(spf), "last_seen": last}
            for key, total, dmarc, dkim, spf, last in rows
        ]

    def dmarc_by_reporter(self, since: datetime) -> list[dict[str, Any]]:
        return self._dmarc_totals(m.DmarcReportRow.reporter, since)

    def dmarc_by_source(self, since: datetime) -> list[dict[str, Any]]:
        return self._dmarc_totals(m.DmarcReportRow.source_ip, since)

    def delete_dmarc_before(self, before: datetime) -> int:
        return _rows(self.s.execute(delete(m.DmarcReportRow).where(m.DmarcReportRow.end_at < before)))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_day(column: Any) -> Any:
    return func.date(func.timezone("UTC", column))


def _domain_of(column: Any) -> Any:
    return func.lower(func.split_part(column, "@", 2))


class SqlAdminMail(_Repo):
    model = m.AdminMail

    def get(self, mail_id: uuid.UUID) -> m.AdminMail | None:
        return self._get(mail_id)

    def list_page(self, page: PageRequest, *, query: str | None) -> Page[m.AdminMail]:
        stmt = select(m.AdminMail)
        if query:
            like = f"%{_escape_like(query)}%"
            stmt = stmt.where(or_(m.AdminMail.subject.ilike(like, escape="\\"),
                                  cast(m.AdminMail.to, Text).ilike(like, escape="\\"),
                                  cast(m.AdminMail.cc, Text).ilike(like, escape="\\")))
        return _paginate(self.s, stmt, m.AdminMail, page)

    def get_attachment(self, attachment_id: uuid.UUID) -> m.AdminMailAttachment | None:
        return self.s.get(m.AdminMailAttachment, attachment_id)

    def attachments_for(self, mail_id: uuid.UUID) -> list[m.AdminMailAttachment]:
        return list(self.s.execute(
            select(m.AdminMailAttachment).where(m.AdminMailAttachment.mail_id == mail_id)
            .order_by(m.AdminMailAttachment.created_at)
        ).scalars())

    def unattached_before(self, before: datetime, limit: int) -> list[m.AdminMailAttachment]:
        return list(self.s.execute(
            select(m.AdminMailAttachment)
            .where(m.AdminMailAttachment.mail_id.is_(None), m.AdminMailAttachment.created_at < before).limit(limit)
        ).scalars())

    def sent_before(self, before: datetime, limit: int) -> list[m.AdminMail]:
        return list(self.s.execute(
            select(m.AdminMail).where(m.AdminMail.created_at < before).limit(limit)
        ).scalars())

    def delete(self, entity: Any) -> None:
        self.s.delete(entity)
        self.s.flush()


class SqlAdminStats(_Repo):
    def _metric(self, metric: str) -> tuple[Any, Any]:
        """(the timestamp column, the base query counting rows) for a dashboard metric."""
        r, e = m.MessageRecipient, m.DeliveryEvent
        count = func.count()
        metrics: dict[str, tuple[Any, Any]] = {
            "sent": (r.created_at, select(count).select_from(r)),
            "delivered": (r.accepted_at, select(count).select_from(r).where(r.accepted_at.is_not(None))),
            "failed": (r.failed_at, select(count).select_from(r).where(r.status == "failed")),
            "bounced": (e.created_at, select(count).select_from(e).where(e.type == "bounce")),
            "hard_bounces": (e.created_at, select(count).select_from(e).where(e.type == "bounce",
                                                                              e.classification == "hard")),
            "complaints": (e.created_at, select(count).select_from(e).where(e.type == "complaint")),
            "signups": (m.User.created_at, select(count).select_from(m.User)),
            "verified": (m.User.email_verified_at, select(count).select_from(m.User)
                         .where(m.User.email_verified_at.is_not(None))),
        }
        return metrics[metric]

    def daily_counts(self, metric: str, since: datetime) -> dict[date, int]:
        if metric == "active_users":
            a = m.UserActivityDay
            rows = self.s.execute(
                select(a.day, func.count()).where(a.day >= since.date()).group_by(a.day)
            ).all()
            return {day: count for day, count in rows}
        column, stmt = self._metric(metric)
        day = _utc_day(column)
        rows = self.s.execute(
            stmt.add_columns(day).where(column >= since).group_by(day)
        ).all()
        return {day_: count for count, day_ in rows}

    def daily_categories(self, since: datetime) -> dict[tuple[date, str], int]:
        r = m.MessageRecipient
        day = _utc_day(r.created_at)
        rows = self.s.execute(
            select(day, m.Message.category, func.count())
            .join(m.Message, m.Message.id == r.message_id)
            .where(r.created_at >= since)
            .group_by(day, m.Message.category)
        ).all()
        return {(day_, category): count for day_, category, count in rows}

    def count_between(self, metric: str, start: datetime, end: datetime) -> int:
        if metric == "active_users":
            a = m.UserActivityDay
            return self.s.execute(
                select(func.count(func.distinct(a.user_id))).where(a.day >= start.date(), a.day < end.date())
            ).scalar_one()
        column, stmt = self._metric(metric)
        return self.s.execute(stmt.where(column >= start, column < end)).scalar_one()

    def recipient_domains(self, since: datetime, limit: int) -> list[tuple[str, int]]:
        r = m.MessageRecipient
        domain = _domain_of(r.email)
        rows = self.s.execute(
            select(domain, func.count()).where(r.accepted_at >= since)
            .group_by(domain).order_by(func.count().desc()).limit(limit)
        ).all()
        return [(d, c) for d, c in rows]

    def event_domains(self, since: datetime) -> list[tuple[str, str, str | None, int]]:
        e = m.DeliveryEvent
        domain = _domain_of(e.email)
        rows = self.s.execute(
            select(domain, e.type, e.classification, func.count()).where(e.created_at >= since)
            .group_by(domain, e.type, e.classification)
        ).all()
        return [(d, t, c, n) for d, t, c, n in rows]

    def account_rates(self, since: datetime) -> list[tuple[uuid.UUID, int, int, int]]:
        r, e = m.MessageRecipient, m.DeliveryEvent
        accepted = (select(r.account_id, func.count().label("n")).where(r.accepted_at >= since)
                    .group_by(r.account_id).subquery())
        hard = (select(e.account_id, func.count().label("n"))
                .where(e.created_at >= since, e.type == "bounce", e.classification == "hard")
                .group_by(e.account_id).subquery())
        complaints = (select(e.account_id, func.count().label("n"))
                      .where(e.created_at >= since, e.type == "complaint").group_by(e.account_id).subquery())
        rows = self.s.execute(
            select(accepted.c.account_id, accepted.c.n, func.coalesce(hard.c.n, 0), func.coalesce(complaints.c.n, 0))
            .outerjoin(hard, hard.c.account_id == accepted.c.account_id)
            .outerjoin(complaints, complaints.c.account_id == accepted.c.account_id)
        ).all()
        return [(a, n, h, c) for a, n, h, c in rows]

    def bounce_reasons(self, since: datetime, limit: int) -> list[tuple[str | None, int, str | None]]:
        e = m.DeliveryEvent
        rows = self.s.execute(
            select(e.status_code, func.count(), func.max(e.diagnostic))
            .where(e.created_at >= since, e.type == "bounce")
            .group_by(e.status_code).order_by(func.count().desc()).limit(limit)
        ).all()
        return [(code, count, sample) for code, count, sample in rows]

    def outbox_summary(self) -> list[tuple[str, str, int]]:
        o = m.OutboxEvent
        rows = self.s.execute(select(o.kind, o.status, func.count()).group_by(o.kind, o.status)).all()
        return [(k, st, c) for k, st, c in rows]

    def oldest_pending_outbox(self) -> datetime | None:
        return self.s.execute(
            select(func.min(m.OutboxEvent.created_at)).where(m.OutboxEvent.status == "pending")
        ).scalar_one()

    def count_audit_since(self, action: str, since: datetime) -> int:
        return self.s.execute(
            select(func.count()).where(m.AuditEvent.action == action, m.AuditEvent.created_at >= since)
        ).scalar_one()

    def users_page(self, page: PageRequest, *, query: str | None, status: str | None,
                   since: datetime) -> Page[tuple[m.User, int, datetime | None, int]]:
        accounts = (select(func.count()).where(m.Membership.user_id == m.User.id)
                    .correlate(m.User).scalar_subquery())
        last_seen = (select(func.max(m.Session.last_seen_at)).where(m.Session.user_id == m.User.id)
                     .correlate(m.User).scalar_subquery())
        sent = (select(func.count()).select_from(m.MessageRecipient)
                .join(m.Message, m.Message.id == m.MessageRecipient.message_id)
                .where(m.Message.created_by_user_id == m.User.id, m.MessageRecipient.created_at >= since)
                .correlate(m.User).scalar_subquery())
        stmt = select(m.User, accounts, last_seen, sent)
        if query:
            stmt = stmt.where(m.User.email.ilike(f"%{_escape_like(query.strip().lower())}%", escape="\\"))
        if status == "blocked":
            stmt = stmt.where(m.User.disabled_at.is_not(None), m.User.deleted_at.is_(None))
        elif status == "deleted":
            stmt = stmt.where(m.User.deleted_at.is_not(None))
        elif status == "unverified":
            stmt = stmt.where(m.User.email_verified_at.is_(None), m.User.disabled_at.is_(None))
        elif status == "active":
            stmt = stmt.where(m.User.email_verified_at.is_not(None), m.User.disabled_at.is_(None))
        return _paginate(self.s, stmt, m.User, page, rows=True)

    def accounts_page(self, page: PageRequest, *, query: str | None, status: str | None,
                      since: datetime) -> Page[tuple[m.Account, str | None, int, int, int]]:
        a = m.Account
        owner = (select(m.User.email).join(m.Membership, m.Membership.user_id == m.User.id)
                 .where(m.Membership.account_id == a.id, m.Membership.role == "owner")
                 .order_by(m.Membership.created_at).limit(1).correlate(a).scalar_subquery())
        members = select(func.count()).where(m.Membership.account_id == a.id).correlate(a).scalar_subquery()
        domains = (select(func.count()).where(m.Domain.account_id == a.id, m.Domain.status == "verified")
                   .correlate(a).scalar_subquery())
        sent_today = (select(func.count()).where(m.MessageRecipient.account_id == a.id,
                                                  m.MessageRecipient.created_at >= since)
                      .correlate(a).scalar_subquery())
        stmt = select(a, owner, members, domains, sent_today)
        if query:
            like = f"%{_escape_like(query.strip().lower())}%"
            owners = (select(m.Membership.account_id).join(m.User, m.User.id == m.Membership.user_id)
                      .where(m.User.email.ilike(like, escape="\\")))
            stmt = stmt.where(or_(func.lower(a.name).like(like, escape="\\"), a.id.in_(owners)))
        if status == "suspended":
            stmt = stmt.where(a.status == "active", a.sending_suspended_at.is_not(None))
        elif status == "deleted":
            stmt = stmt.where(a.status != "active")
        elif status == "active":
            stmt = stmt.where(a.status == "active", a.sending_suspended_at.is_(None))
        return _paginate(self.s, stmt, a, page, rows=True)

    def user_accounts(self, user_id: uuid.UUID) -> list[tuple[m.Account, str]]:
        rows = self.s.execute(
            select(m.Account, m.Membership.role).join(m.Membership, m.Membership.account_id == m.Account.id)
            .where(m.Membership.user_id == user_id).order_by(m.Account.created_at)
        ).all()
        return [(account, role) for account, role in rows]

    def owned_accounts(self, user_id: uuid.UUID) -> list[m.Account]:
        return [account for account, role in self.user_accounts(user_id) if role == "owner"]

    def account_members(self, account_id: uuid.UUID) -> list[tuple[m.User, str]]:
        rows = self.s.execute(
            select(m.User, m.Membership.role).join(m.Membership, m.Membership.user_id == m.User.id)
            .where(m.Membership.account_id == account_id).order_by(m.Membership.created_at)
        ).all()
        return [(user, role) for user, role in rows]

    def owner_emails(self, account_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not account_ids:
            return {}
        rows = self.s.execute(
            select(m.Membership.account_id, m.User.email).join(m.User, m.User.id == m.Membership.user_id)
            .where(m.Membership.account_id.in_(list(account_ids)), m.Membership.role == "owner")
            .order_by(m.Membership.created_at.desc())
        ).all()
        return {account_id: email for account_id, email in rows}

    def accounts_by_ids(self, account_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, m.Account]:
        if not account_ids:
            return {}
        rows = self.s.execute(select(m.Account).where(m.Account.id.in_(list(account_ids)))).scalars()
        return {account.id: account for account in rows}

    def recent_events(self, account_id: uuid.UUID, limit: int) -> list[m.DeliveryEvent]:
        return list(self.s.execute(
            select(m.DeliveryEvent).where(m.DeliveryEvent.account_id == account_id)
            .order_by(m.DeliveryEvent.created_at.desc()).limit(limit)
        ).scalars())

    def count_suppressions(self, account_id: uuid.UUID) -> int:
        return self.s.execute(select(func.count()).where(m.Suppression.account_id == account_id)).scalar_one()

    def count_active_sessions(self, user_id: uuid.UUID, now: datetime) -> int:
        return self.s.execute(
            select(func.count()).where(m.Session.user_id == user_id, m.Session.revoked_at.is_(None),
                                       m.Session.expires_at > now)
        ).scalar_one()

    def last_seen(self, user_id: uuid.UUID) -> datetime | None:
        return self.s.execute(
            select(func.max(m.Session.last_seen_at)).where(m.Session.user_id == user_id)
        ).scalar_one()

    def unverified_users(self, created_before: datetime, limit: int) -> list[m.User]:
        return list(self.s.execute(
            select(m.User).where(m.User.email_verified_at.is_(None), m.User.disabled_at.is_(None),
                                 m.User.created_at < created_before)
            .order_by(m.User.created_at.desc()).limit(limit)
        ).scalars())

    def pending_domains(self, created_before: datetime, limit: int) -> list[m.Domain]:
        return list(self.s.execute(
            select(m.Domain).join(m.Account, m.Account.id == m.Domain.account_id)
            .where(m.Domain.status == "pending", m.Domain.disabled_at.is_(None), m.Account.status == "active",
                   or_(m.Domain.created_at < created_before, m.Domain.verified_at.is_not(None)))
            .order_by(m.Domain.created_at.desc()).limit(limit)
        ).scalars())

    def pending_senders(self, created_before: datetime, limit: int) -> list[m.SenderIdentity]:
        return list(self.s.execute(
            select(m.SenderIdentity).join(m.Account, m.Account.id == m.SenderIdentity.account_id)
            .where(m.SenderIdentity.status == "pending", m.SenderIdentity.disabled_at.is_(None),
                   m.SenderIdentity.created_at < created_before, m.Account.status == "active")
            .order_by(m.SenderIdentity.created_at.desc()).limit(limit)
        ).scalars())

    def suspended_accounts(self, limit: int) -> list[m.Account]:
        return list(self.s.execute(
            select(m.Account).where(m.Account.status == "active", m.Account.sending_suspended_at.is_not(None))
            .order_by(m.Account.sending_suspended_at.desc()).limit(limit)
        ).scalars())

    def limit_violations(self, since: datetime) -> list[tuple[uuid.UUID, int]]:
        prefix = "limit_violation:"
        rows = self.s.execute(
            select(m.RateLimitCounter.key, func.sum(m.RateLimitCounter.count))
            .where(m.RateLimitCounter.key.like(prefix + "%"), m.RateLimitCounter.window_start >= since)
            .group_by(m.RateLimitCounter.key)
        ).all()
        result = []
        for key, count in rows:
            try:
                result.append((uuid.UUID(key.removeprefix(prefix)), int(count)))
            except ValueError:
                continue
        return result

    def dead_deliveries(self, since: datetime) -> list[tuple[uuid.UUID, int, str | None]]:
        r = m.MessageRecipient
        rows = self.s.execute(
            select(r.account_id, func.count(), func.max(r.last_error))
            .where(r.status == "failed", r.failed_at >= since).group_by(r.account_id)
        ).all()
        return [(a, c, e) for a, c, e in rows]

    def bulk_without_address(self, limit: int) -> list[m.Account]:
        eligible = select(m.Domain.account_id).where(m.Domain.bulk_eligible.is_(True), m.Domain.disabled_at.is_(None))
        return list(self.s.execute(
            select(m.Account).where(m.Account.status == "active", m.Account.id.in_(eligible),
                                    or_(m.Account.physical_address.is_(None), m.Account.physical_address == ""))
            .order_by(m.Account.created_at.desc()).limit(limit)
        ).scalars())

    def operator_audit_page(self, page: PageRequest, *, actor_id: uuid.UUID | None,
                            action: str | None) -> Page[m.AuditEvent]:
        stmt = select(m.AuditEvent).where(m.AuditEvent.actor_type == "operator")
        if actor_id:
            stmt = stmt.where(m.AuditEvent.actor_id == actor_id)
        if action:
            stmt = stmt.where(m.AuditEvent.action.like(f"{_escape_like(action)}%", escape="\\"))
        return _paginate(self.s, stmt, m.AuditEvent, page)


class SqlUnitOfWork:
    def __init__(self, session: Session):
        self.session = session
        self.users = SqlUsers(session)
        self.sessions = SqlSessions(session)
        self.email_tokens = SqlEmailTokens(session)
        self.accounts = SqlAccounts(session)
        self.memberships = SqlMemberships(session)
        self.invitations = SqlInvitations(session)
        self.api_keys = SqlApiKeys(session)
        self.domains = SqlDomains(session)
        self.dkim_keys = SqlDkimKeys(session)
        self.platform_dkim_keys = SqlPlatformDkimKeys(session)
        self.sender_identities = SqlSenderIdentities(session)
        self.assets = SqlAssets(session)
        self.messages = SqlMessages(session)
        self.outbox = SqlOutbox(session)
        self.delivery = SqlDelivery(session)
        self.suppressions = SqlSuppressions(session)
        self.audit = SqlAudit(session)
        self.rate_limits = SqlRateLimits(session)
        self.sync = SqlSync(session)
        self.exports = SqlExports(session)
        self.jobs = SqlJobs(session)
        self.admins = SqlAdmins(session)
        self.admin_issues = SqlAdminIssues(session)
        self.inbound = SqlInbound(session)
        self.admin_stats = SqlAdminStats(session)
        self.admin_mail = SqlAdminMail(session)

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def savepoint(self) -> Any:
        return self.session.begin_nested()

"""SQLAlchemy implementations of the repository interfaces."""

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Select, and_, delete, func, or_, select, text, tuple_, update
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
        rows = self.s.execute(
            select(m.DkimKey, m.Domain)
            .join(m.Domain, m.Domain.id == m.DkimKey.domain_id)
            .where(m.DkimKey.status == "active", m.Domain.disabled_at.is_(None))
        ).all()
        return [(key, domain) for key, domain in rows]

    def delete_retired_before(self, before: datetime) -> int:
        return _rows(self.s.execute(
            delete(m.DkimKey).where(m.DkimKey.status == "retired", m.DkimKey.retired_at < before)
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
        return _rows(self.s.execute(delete(m.SyncRecord).where(m.SyncRecord.account_id == account_id)))


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

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()

    def savepoint(self) -> Any:
        return self.session.begin_nested()

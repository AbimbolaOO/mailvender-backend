"""Accounts, memberships, invitations and API keys."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from app import models as m
from app.config import get_settings
from app.errors import ApiError, Conflict, Forbidden, NotFound, Unauthenticated
from app.logging import account_id_var, get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import generate_api_key, hash_token, new_token, utcnow
from app.services import emails
from app.services.common import AccountContext, Actor, app_url, audit, enqueue_system_email, normalize_email

log = get_logger("accounts")

ROLES = ("owner", "member")


@dataclass
class CreatedApiKey:
    key: m.ApiKey
    plaintext: str


class AccountService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow
        self.settings = get_settings()

    # ---- Context resolution (used by every account-scoped request) ----

    def context_for_user(self, user: m.User, account_id: uuid.UUID) -> AccountContext:
        membership = self.uow.memberships.get(account_id, user.id)
        account = self.uow.accounts.get(account_id) if membership else None
        if membership is None or account is None or account.status != "active":
            # Same answer whether the account exists or not.
            raise Forbidden("You're not a member of this account.", code="not_a_member")
        account_id_var.set(str(account.id))
        return AccountContext(account=account, role=membership.role, user=user)

    def context_for_api_key(self, plaintext: str) -> AccountContext:
        key = self.uow.api_keys.get_by_hash(hash_token(plaintext))
        if key is None or key.revoked_at is not None:
            raise Unauthenticated("This API key is invalid or revoked.", code="api_key_invalid")
        account = self.uow.accounts.get(key.account_id)
        if account is None or account.status != "active":
            raise Unauthenticated("This API key is invalid or revoked.", code="api_key_invalid")
        now = utcnow()
        if key.last_used_at is None or now - key.last_used_at > timedelta(minutes=1):
            key.last_used_at = now
            self.uow.commit()
        account_id_var.set(str(account.id))
        return AccountContext(account=account, role="member", api_key=key)

    # ---- Accounts ----

    def list_for_user(self, user: m.User) -> list[tuple[m.Account, str]]:
        return self.uow.accounts.list_for_user(user.id)

    def create_team(self, user: m.User, name: str) -> m.Account:
        account = self.uow.accounts.add(
            m.Account(
                name=name.strip(),
                kind="team",
                hourly_recipient_limit=self.settings.default_hourly_recipient_limit,
                daily_recipient_limit=self.settings.default_daily_recipient_limit,
            )
        )
        self.uow.memberships.add(m.Membership(account_id=account.id, user_id=user.id, role="owner"))
        audit(self.uow, account.id, Actor("user", user.id), "account.created", target_type="account",
              target_id=account.id, kind="team")
        audit(self.uow, account.id, Actor.system(), "account.limits_set", target_type="account",
              target_id=account.id, reason="Default limits for new accounts",
              hourly=account.hourly_recipient_limit, daily=account.daily_recipient_limit)
        self.uow.commit()
        return account

    def update_settings(self, ctx: AccountContext, *, name: str | None, physical_address: str | None) -> m.Account:
        account = ctx.account
        if name is not None:
            if account.kind == "personal" and name.strip() != account.name:
                ctx.require_user()
            account.name = name.strip()
        if physical_address is not None:
            account.physical_address = physical_address.strip() or None
        audit(self.uow, account.id, ctx.actor, "account.settings_updated", target_type="account",
              target_id=account.id, name=name, physical_address_set=physical_address is not None)
        self.uow.commit()
        return account

    # ---- Members ----

    def list_members(self, ctx: AccountContext, page: PageRequest) -> Page[tuple[m.Membership, m.User]]:
        return self.uow.memberships.list_for_account(ctx.account_id, page)

    def _require_not_last_owner(self, membership: m.Membership) -> None:
        if membership.role == "owner" and self.uow.memberships.count_owners(membership.account_id) <= 1:
            raise Conflict(
                "The last owner can't leave or be removed. Transfer ownership to another member first.",
                code="last_owner",
            )

    def remove_member(self, ctx: AccountContext, user_id: uuid.UUID) -> None:
        actor_user = ctx.require_user()
        if user_id == actor_user.id:
            return self.leave(ctx)
        ctx.require_owner()
        membership = self.uow.memberships.get(ctx.account_id, user_id, for_update=True)
        if membership is None:
            raise NotFound("Member not found.")
        self._require_not_last_owner(membership)
        self.uow.memberships.delete(membership)
        audit(self.uow, ctx.account_id, ctx.actor, "member.removed", target_type="user", target_id=user_id)
        self.uow.commit()

    def leave(self, ctx: AccountContext) -> None:
        user = ctx.require_user()
        membership = self.uow.memberships.get(ctx.account_id, user.id, for_update=True)
        if membership is None:
            raise NotFound("Member not found.")
        self._require_not_last_owner(membership)
        self.uow.memberships.delete(membership)
        if user.default_account_id == ctx.account_id:
            others = [a for a, _ in self.uow.accounts.list_for_user(user.id) if a.id != ctx.account_id]
            user.default_account_id = others[0].id if others else None
        audit(self.uow, ctx.account_id, ctx.actor, "member.left", target_type="user", target_id=user.id)
        self.uow.commit()

    def transfer_ownership(self, ctx: AccountContext, to_user_id: uuid.UUID) -> None:
        ctx.require_owner()
        current = ctx.require_user()
        if to_user_id == current.id:
            raise Conflict("You already own this account.", code="already_owner")
        target = self.uow.memberships.get(ctx.account_id, to_user_id, for_update=True)
        mine = self.uow.memberships.get(ctx.account_id, current.id, for_update=True)
        if target is None or mine is None:
            raise NotFound("Member not found.")
        target.role = "owner"
        mine.role = "member"
        audit(self.uow, ctx.account_id, ctx.actor, "ownership.transferred", target_type="user",
              target_id=to_user_id, from_user_id=str(current.id))
        self.uow.commit()

    # ---- Invitations ----

    def invite(self, ctx: AccountContext, email: str) -> m.Invitation:
        ctx.require_owner()
        if ctx.account.kind == "personal":
            raise Conflict("Personal accounts can't have other members. Create a team account.",
                           code="personal_account")
        normalized = normalize_email(email)
        existing_user = self.uow.users.get_by_email(normalized)
        if existing_user and self.uow.memberships.get(ctx.account_id, existing_user.id):
            raise Conflict("This person is already a member.", code="already_member")
        token = new_token()
        invitation = self.uow.invitations.add(
            m.Invitation(
                account_id=ctx.account_id,
                email=normalized,
                role="member",
                token_hash=hash_token(token),
                invited_by=ctx.require_user().id,
                expires_at=utcnow() + timedelta(hours=self.settings.invitation_ttl_hours),
            )
        )
        enqueue_system_email(
            self.uow,
            normalized,
            emails.invitation(ctx.require_user().email, ctx.account.name, normalized,
                              app_url("/invite?token=" + token), self.settings.invitation_ttl_hours // 24),
            kind="invitation",
        )
        audit(self.uow, ctx.account_id, ctx.actor, "invitation.created", target_type="invitation",
              target_id=invitation.id, email=normalized)
        self.uow.commit()
        return invitation

    def list_invitations(self, ctx: AccountContext, page: PageRequest) -> Page[m.Invitation]:
        ctx.require_owner()
        return self.uow.invitations.list_pending(ctx.account_id, utcnow(), page)

    def revoke_invitation(self, ctx: AccountContext, invitation_id: uuid.UUID) -> None:
        ctx.require_owner()
        invitation = self.uow.invitations.get(invitation_id, ctx.account_id)
        if invitation is None:
            raise NotFound("Invitation not found.")
        if invitation.accepted_at:
            raise Conflict("This invitation was already accepted.", code="invitation_used")
        if invitation.revoked_at is None:
            invitation.revoked_at = utcnow()
            audit(self.uow, ctx.account_id, ctx.actor, "invitation.revoked", target_type="invitation",
                  target_id=invitation.id)
        self.uow.commit()

    def accept_invitation(self, user: m.User, token: str) -> m.Account:
        invalid = ApiError("This invitation is invalid, expired or already used.", code="invalid_invitation",
                           status_code=400)
        invitation = self.uow.invitations.get_by_hash(hash_token(token), for_update=True)
        now = utcnow()
        if (
            invitation is None
            or invitation.accepted_at is not None
            or invitation.revoked_at is not None
            or invitation.expires_at <= now
        ):
            raise invalid
        if invitation.email != user.email:
            raise Forbidden("This invitation was sent to a different email address.", code="invitation_email_mismatch")
        account = self.uow.accounts.get(invitation.account_id)
        if account is None or account.status != "active":
            raise invalid
        if self.uow.memberships.get(account.id, user.id):
            raise Conflict("You're already a member of this account.", code="already_member")
        self.uow.memberships.add(m.Membership(account_id=account.id, user_id=user.id, role=invitation.role))
        invitation.accepted_at = now
        invitation.accepted_by = user.id
        audit(self.uow, account.id, Actor("user", user.id), "invitation.accepted", target_type="invitation",
              target_id=invitation.id)
        self.uow.commit()
        log_event(log, "invitation.accepted", account_id=str(account.id))
        return account

    # ---- API keys ----

    def create_api_key(self, ctx: AccountContext, name: str) -> CreatedApiKey:
        ctx.require_owner()
        generated = generate_api_key()
        key = self.uow.api_keys.add(
            m.ApiKey(
                account_id=ctx.account_id,
                name=name.strip(),
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                created_by=ctx.require_user().id,
            )
        )
        audit(self.uow, ctx.account_id, ctx.actor, "api_key.created", target_type="api_key", target_id=key.id,
              name=key.name)
        self.uow.commit()
        return CreatedApiKey(key=key, plaintext=generated.plaintext)

    def list_api_keys(self, ctx: AccountContext, page: PageRequest) -> Page[m.ApiKey]:
        ctx.require_owner()
        return self.uow.api_keys.list_page(ctx.account_id, page)

    def revoke_api_key(self, ctx: AccountContext, key_id: uuid.UUID) -> m.ApiKey:
        ctx.require_owner()
        key = self.uow.api_keys.get(key_id, ctx.account_id)
        if key is None:
            raise NotFound("API key not found.")
        if key.revoked_at is None:
            key.revoked_at = utcnow()
            audit(self.uow, ctx.account_id, ctx.actor, "api_key.revoked", target_type="api_key", target_id=key.id)
        self.uow.commit()
        return key

    def audit_log(self, ctx: AccountContext, page: PageRequest) -> Page[m.AuditEvent]:
        ctx.require_owner()
        return self.uow.audit.list_page(ctx.account_id, page)


def role_allows(role: str, required: str) -> bool:
    return required == "member" or role == "owner"

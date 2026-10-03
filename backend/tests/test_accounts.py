from datetime import timedelta

from app import models as m
from app.security import hash_token, utcnow
from tests.conftest import CSRF, last_link_token


def team(owner, name="Acme"):  # type: ignore[no-untyped-def]
    response = owner.post("/v1/accounts", json={"name": name})
    assert response.status_code == 201
    owner.account_id = response.json()["id"]
    return owner.account_id


def invite_and_accept(owner, member):  # type: ignore[no-untyped-def]
    response = owner.post("/v1/account/invitations", json={"email": member.email})
    assert response.status_code == 201, response.text
    token = last_link_token(member.email, "invitation")
    response = member.post("/v1/invitations/accept", json={"token": token})
    assert response.status_code == 200, response.text
    return token


def test_accounts_list_and_switching(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    personal = user.account_id
    team_id = team(user)
    accounts = user.get("/v1/accounts").json()
    assert [a["kind"] for a in accounts] == ["personal", "team"]
    assert accounts[0]["id"] == personal
    assert user.get("/v1/account").json()["id"] == team_id
    user.account_id = personal
    assert user.get("/v1/account").json()["kind"] == "personal"


def test_missing_or_foreign_account_header(make_user) -> None:  # type: ignore[no-untyped-def]
    alice, bob = make_user(), make_user()
    response = alice.client.get("/v1/account", headers=CSRF)
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["field"] == "X-Account-Id"
    response = alice.get("/v1/account", headers={"X-Account-Id": bob.account_id})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_a_member"


def test_invitation_flow_is_single_use(make_user, db) -> None:  # type: ignore[no-untyped-def]
    owner, member = make_user(), make_user()
    team(owner)
    token = invite_and_accept(owner, member)
    member.account_id = owner.account_id
    assert member.get("/v1/account").json()["role"] == "member"
    again = member.post("/v1/invitations/accept", json={"token": token})
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "invalid_invitation"
    memberships = db.session.query(m.Membership).filter_by(account_id=owner.account_id).count()
    assert memberships == 2


def test_invitation_requires_matching_email_and_can_be_revoked(make_user, db) -> None:  # type: ignore[no-untyped-def]
    owner, invited, stranger = make_user(), make_user(), make_user()
    team(owner)
    invitation = owner.post("/v1/account/invitations", json={"email": invited.email}).json()
    token = last_link_token(invited.email, "invitation")
    response = stranger.post("/v1/invitations/accept", json={"token": token})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invitation_email_mismatch"

    assert owner.delete(f"/v1/account/invitations/{invitation['id']}").status_code == 204
    assert invited.post("/v1/invitations/accept", json={"token": token}).status_code == 400
    assert owner.get("/v1/account/invitations").json()["items"] == []


def test_expired_invitation_fails_without_membership(make_user, db) -> None:  # type: ignore[no-untyped-def]
    owner, invited = make_user(), make_user()
    team(owner)
    owner.post("/v1/account/invitations", json={"email": invited.email})
    token = last_link_token(invited.email, "invitation")
    invitation = db.session.query(m.Invitation).filter_by(token_hash=hash_token(token)).one()
    invitation.expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    assert invited.post("/v1/invitations/accept", json={"token": token}).status_code == 400
    assert db.session.query(m.Membership).filter_by(user_id=invited.user_id, account_id=owner.account_id).count() == 0


def test_member_permissions(make_user) -> None:  # type: ignore[no-untyped-def]
    owner, member = make_user(), make_user()
    team(owner)
    invite_and_accept(owner, member)
    member.account_id = owner.account_id
    for response in (
        member.post("/v1/account/invitations", json={"email": "x@example.com"}),
        member.post("/v1/account/api-keys", json={"name": "k"}),
        member.get("/v1/account/api-keys"),
        member.delete(f"/v1/account/members/{owner.user_id}"),
        member.post("/v1/account/transfer-ownership", json={"user_id": member.user_id}),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "owner_required"
    # Members manage domains and sending settings.
    assert member.post("/v1/domains", json={"name": "member-domain.example"}).status_code == 201
    assert member.patch("/v1/account", json={"physical_address": "1 Main St"}).status_code == 200


def test_last_owner_cannot_leave_until_ownership_transferred(make_user) -> None:  # type: ignore[no-untyped-def]
    owner, member = make_user(), make_user()
    team(owner)
    invite_and_accept(owner, member)
    response = owner.post("/v1/account/leave")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_owner"
    assert owner.delete(f"/v1/account/members/{owner.user_id}").status_code == 409

    assert owner.post("/v1/account/transfer-ownership", json={"user_id": member.user_id}).status_code == 204
    member.account_id = owner.account_id
    assert member.get("/v1/account").json()["role"] == "owner"
    assert owner.post("/v1/account/leave").status_code == 204
    assert owner.get("/v1/account").status_code == 403


def test_owner_removes_member(make_user) -> None:  # type: ignore[no-untyped-def]
    owner, member = make_user(), make_user()
    team(owner)
    invite_and_accept(owner, member)
    assert owner.delete(f"/v1/account/members/{member.user_id}").status_code == 204
    member.account_id = owner.account_id
    assert member.get("/v1/account").status_code == 403
    members = owner.get("/v1/account/members").json()["items"]
    assert [x["user_id"] for x in members] == [owner.user_id]


def test_personal_accounts_cannot_invite(make_user) -> None:  # type: ignore[no-untyped-def]
    owner = make_user()
    response = owner.post("/v1/account/invitations", json={"email": "x@example.com"})
    assert response.json()["error"]["code"] == "personal_account"


def test_api_keys_shown_once_hashed_and_revocable(make_user, db) -> None:  # type: ignore[no-untyped-def]
    owner = make_user()
    created = owner.post("/v1/account/api-keys", json={"name": "CI"}).json()
    secret = created["key"]
    assert secret.startswith("mv_")
    stored = db.session.query(m.ApiKey).one()
    assert stored.key_hash == hash_token(secret) and secret not in stored.key_hash
    listed = owner.get("/v1/account/api-keys").json()["items"]
    assert "key" not in listed[0] and listed[0]["prefix"] == created["prefix"]

    api = {"Authorization": f"Bearer {secret}"}
    assert owner.client.get("/v1/messages", headers=api).status_code == 200
    assert owner.delete(f"/v1/account/api-keys/{created['id']}").json()["revoked_at"]
    response = owner.client.get("/v1/messages", headers=api)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api_key_invalid"


def test_api_keys_cannot_manage_the_account(make_user) -> None:  # type: ignore[no-untyped-def]
    owner = make_user()
    secret = owner.post("/v1/account/api-keys", json={"name": "CI"}).json()["key"]
    api = {"Authorization": f"Bearer {secret}", **CSRF}
    owner.client.cookies.clear()
    assert owner.client.post("/v1/account/api-keys", json={"name": "x"}, headers=api).status_code == 403
    assert owner.client.get("/v1/account/members", headers=api).status_code == 403


def test_list_pagination_is_bounded(make_user) -> None:  # type: ignore[no-untyped-def]
    owner = make_user()
    for index in range(3):
        owner.post("/v1/account/api-keys", json={"name": f"k{index}"})
    first = owner.get("/v1/account/api-keys?limit=2").json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = owner.get(f"/v1/account/api-keys?limit=2&cursor={first['next_cursor']}").json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None
    assert {k["name"] for k in first["items"] + second["items"]} == {"k0", "k1", "k2"}
    assert owner.get("/v1/account/api-keys?limit=101").status_code == 422

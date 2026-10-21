import io
import json
import uuid
import zipfile
from datetime import timedelta

from app import models as m
from app.security import utcnow
from tests.conftest import PASSWORD, login, png_bytes, register, send, verified_sender


def test_export_is_built_downloaded_and_audited(make_user, resolver, worker, storage, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_sync import put_dataset, upload_content

    user = make_user()
    setup = verified_sender(user, resolver)
    send(user, setup["identity"]["email"], ["r@example.org"])
    user.post("/v1/suppressions", json={"email": "no@example.org"})
    user.put(f"/v1/sync/brand/{uuid.uuid4()}", json={"expected_revision": 0, "data": {"linkColor": "#f00"}})
    dataset = str(uuid.uuid4())
    upload_id, _ = upload_content(user, storage, dataset, b'{"rows":[["a"]]}')
    put_dataset(user, dataset, 0, upload_id)
    image = add_image(user, storage)

    export = user.post("/v1/account/exports").json()
    assert export["status"] == "pending"
    worker.process_outbox()
    exports = user.get("/v1/account/exports").json()["items"]
    assert exports[0]["status"] == "ready" and exports[0]["expires_at"]

    response = user.get(f"/v1/account/exports/{export['id']}/download")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert json.loads(archive.read("workspace/brand.json"))[0]["data"] == {"linkColor": "#f00"}
    datasets = json.loads(archive.read("workspace/datasets.json"))
    assert json.loads(archive.read(datasets[0]["content_file"])) == {"rows": [["a"]]}
    assert json.loads(archive.read("suppressions.json"))[0]["email"] == "no@example.org"
    messages = json.loads(archive.read("messages.json"))
    assert messages[0]["recipients"][0]["email"] == "r@example.org" and "html" not in messages[0]
    assert "private" not in archive.read("domains.json").decode().lower()
    assets = json.loads(archive.read("assets.json"))
    assert assets[0]["id"] == image["id"] and archive.read(assets[0]["file"]) == png_bytes()

    link = user.post(f"/v1/account/exports/{export['id']}/link").json()
    assert link["expires_in"] == 300 and f"filename=mailvender-export-{export['id']}.zip" in link["url"]

    actions = [e["action"] for e in user.get("/v1/account/audit-events").json()["items"]]
    assert {"export.requested", "export.completed", "export.downloaded"} <= set(actions)

    record = db.session.get(m.AccountExport, export["id"])
    record.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert user.get(f"/v1/account/exports/{export['id']}/download").status_code == 410


def add_image(user, storage) -> dict:  # type: ignore[no-untyped-def]
    upload = user.post("/v1/assets/uploads", json={"filename": "a.png", "content_type": "image/png",
                                                    "size": 100}).json()
    storage.objects[storage.presigned[-1]["key"]] = (png_bytes(), "image/png")
    return user.post(f"/v1/assets/{upload['asset']['id']}/finalize").json()


def test_members_cannot_export(make_user) -> None:  # type: ignore[no-untyped-def]
    from tests.test_accounts import invite_and_accept, team

    owner, member = make_user(), make_user()
    team(owner)
    invite_and_accept(owner, member)
    member.account_id = owner.account_id
    assert member.post("/v1/account/exports").status_code == 403


def test_account_deletion(make_user, resolver, worker, transport, storage, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    key = user.post("/v1/account/api-keys", json={"name": "k"}).json()["key"]
    message = send(user, setup["identity"]["email"], ["r@example.org"]).json()
    asset = add_image(user, storage)
    from tests.test_sync import put_dataset, upload_content

    dataset = str(uuid.uuid4())
    put_dataset(user, dataset, 0, upload_content(user, storage, dataset, b"{}")[0])
    upload_content(user, storage, str(uuid.uuid4()), b"{}")  # never committed

    wrong = user.post("/v1/account/deletion", json={"confirm_name": "nope"})
    assert wrong.status_code == 422
    assert user.post("/v1/account/deletion", json={"confirm_name": "Personal"}).status_code == 202

    # Sessions and keys are revoked …
    assert user.get("/v1/auth/me").status_code == 401
    assert user.client.get("/v1/messages", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    # … pending sends stopped, domains and senders disabled; images and data kept until the purge.
    recipient = db.session.query(m.MessageRecipient).filter_by(message_id=message["id"]).one()
    assert recipient.status == "failed"
    worker.process_outbox()
    assert [to for _, to, _ in transport.sent] == [[user.email]]  # only the "scheduled for deletion" notice
    domain = db.session.get(m.Domain, setup["domain"]["id"])
    assert domain.status == "disabled" and domain.disabled_at
    assert db.session.get(m.SenderIdentity, setup["identity"]["id"]).status == "disabled"
    assert db.session.get(m.Asset, asset["id"]).status == "ready"
    actions = [e.action for e in db.session.query(m.AuditEvent).filter_by(account_id=user.account_id)]
    assert "account.deletion_requested" in actions
    assert b"will be deleted on" in transport.sent[0][2]

    # The domain can be registered again by someone else.
    other = make_user()
    assert other.post("/v1/domains", json={"name": setup["domain"]["name"]}).status_code == 201

    # After the retention period the purge erases images, workspace data and memberships.
    db.session.query(m.Account).filter_by(id=user.account_id).update(
        {"deletion_requested_at": utcnow() - timedelta(days=31)})
    db.commit()
    worker.run_job(next(j for j in worker.jobs if j.name == "retention"))
    db.session.expire_all()
    assert db.session.get(m.Asset, asset["id"]).status == "deleted"
    assert not any("/public/" in k or "/sheets/" in k for k in storage.objects)
    assert db.session.query(m.Membership).filter_by(account_id=user.account_id).count() == 0
    assert db.session.get(m.Account, user.account_id).deleted_at


def test_deleted_account_can_be_restored(make_user, resolver, storage, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_accounts import invite_and_accept, team

    owner, member = make_user(), make_user()
    team(owner)
    invite_and_accept(owner, member)
    setup = verified_sender(owner, resolver)
    image = add_image(owner, storage)
    account_id = owner.account_id
    assert owner.post("/v1/account/deletion", json={"confirm_name": "Acme"}).status_code == 202

    owner = login(owner.client, owner.email)
    me = owner.get("/v1/auth/me").json()
    assert [a["id"] for a in me["accounts"]] == [owner.account_id]  # only the personal one
    assert me["deleted_accounts"][0]["id"] == account_id and me["deleted_accounts"][0]["purge_on"]
    member = login(member.client, member.email)
    assert member.get("/v1/auth/me").json()["deleted_accounts"] == []  # members can't restore
    assert member.post(f"/v1/accounts/{account_id}/restore").status_code == 403

    restored = owner.post(f"/v1/accounts/{account_id}/restore")
    assert restored.status_code == 200 and restored.json()["role"] == "owner"
    assert owner.post(f"/v1/accounts/{account_id}/restore").status_code == 404
    member.account_id = owner.account_id = account_id
    assert member.get("/v1/account").status_code == 200  # everyone is back
    domain = owner.get(f"/v1/domains/{setup['domain']['id']}").json()
    assert domain["status"] == "pending"
    assert owner.post(f"/v1/domains/{setup['domain']['id']}/verify").json()["status"] == "verified"
    assert owner.get("/v1/sender-identities").json()["items"][0]["status"] == "pending"
    assert db.session.get(m.Asset, image["id"]).status == "ready"
    actions = [e["action"] for e in owner.get("/v1/account/audit-events").json()["items"]]
    assert "account.restored" in actions


# ---- The person ----


def test_personal_export(make_user, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_accounts import team

    user = make_user()
    team(user, "Side project")
    response = user.post("/v1/auth/me/export")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    profile = json.loads(archive.read("profile.json"))
    assert profile["email"] == user.email and "password_hash" not in profile
    assert {a["name"] for a in json.loads(archive.read("accounts.json"))} == {"Personal", "Side project"}
    assert json.loads(archive.read("sessions.json"))
    assert "account.created" in {e["action"] for e in json.loads(archive.read("activity.json"))}
    assert db.session.query(m.AuditEvent).filter_by(action="user.exported").count() == 1
    assert user.client.post("/v1/auth/me/export").status_code == 403  # needs the CSRF header


def test_delete_login(make_user, db) -> None:  # type: ignore[no-untyped-def]
    from tests.test_accounts import invite_and_accept, team

    user, colleague = make_user(), make_user()
    personal = user.account_id
    team(colleague, "Their team")
    invite_and_accept(colleague, user)
    shared = team(user, "Shared")
    invite_and_accept(user, colleague)

    assert user.post("/v1/auth/me/deletion", json={"password": "wrong"}).status_code == 422
    blocked = user.post("/v1/auth/me/deletion", json={"password": PASSWORD})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "owns_shared_accounts"
    assert blocked.json()["error"]["details"]["accounts"] == [{"id": shared, "name": "Shared"}]

    user.post("/v1/account/transfer-ownership", json={"user_id": colleague.user_id})
    response = user.post("/v1/auth/me/deletion", json={"password": PASSWORD})
    assert response.status_code == 204, response.text
    assert user.get("/v1/auth/me").status_code == 401

    db.session.expire_all()
    row = db.session.get(m.User, user.user_id)
    assert row.deleted_at and row.email.endswith("@deleted.invalid")
    assert db.session.query(m.Membership).filter_by(user_id=row.id).count() == 0
    assert db.session.get(m.Account, personal).status == "deleted"  # theirs alone: deleted
    assert db.session.get(m.Account, shared).status == "active"  # now the colleague's
    emails = [e.payload["template"] for e in db.session.query(m.OutboxEvent).filter(
        m.OutboxEvent.kind == "system_email", m.OutboxEvent.status == "pending") if e.payload["to"] == user.email]
    assert emails == ["invitation", "login_deleted"]  # one goodbye, no "restore your workspace" email
    assert db.session.query(m.AuditEvent).filter_by(action="user.deleted", target_id=row.id).count() == 1

    # The address is free again.
    register(user.client, user.email)
    assert login(user.client, user.email).user_id != user.user_id


def test_retention_job(make_user, resolver, worker, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    message_id = send(user, setup["identity"]["email"], ["r@example.org"]).json()["id"]
    user.post("/v1/suppressions", json={"email": "keep@example.org"})
    worker.process_outbox()
    old = utcnow() - timedelta(days=40)
    db.session.execute(m.Message.__table__.update().values(created_at=old))
    db.commit()
    worker.run_job(next(j for j in worker.jobs if j.name == "retention"))
    db.session.expire_all()
    message = db.session.get(m.Message, message_id)
    assert message.html is None and message.text_body is None and message.content_purged_at
    assert user.get(f"/v1/messages/{message_id}").json()["recipients"][0]["status"] == "accepted_by_mta"

    db.session.execute(m.Message.__table__.update().values(created_at=utcnow() - timedelta(days=100)))
    db.commit()
    worker.run_job(next(j for j in worker.jobs if j.name == "retention"))
    db.session.expire_all()
    assert db.session.get(m.Message, message_id) is None
    assert db.session.query(m.Suppression).count() == 1  # suppressions are kept

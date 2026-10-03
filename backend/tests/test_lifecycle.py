import io
import json
import uuid
import zipfile
from datetime import timedelta

from app import models as m
from app.security import utcnow
from tests.conftest import send, verified_sender


def test_export_is_built_downloaded_and_audited(make_user, resolver, worker, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    send(user, setup["identity"]["email"], ["r@example.org"])
    user.post("/v1/suppressions", json={"email": "no@example.org"})
    user.put(f"/v1/sync/brand/{uuid.uuid4()}", json={"expected_revision": 0, "data": {"linkColor": "#f00"}})

    export = user.post("/v1/account/exports").json()
    assert export["status"] == "pending"
    worker.process_outbox()
    exports = user.get("/v1/account/exports").json()["items"]
    assert exports[0]["status"] == "ready" and exports[0]["expires_at"]

    response = user.get(f"/v1/account/exports/{export['id']}/download")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert json.loads(archive.read("workspace/brand.json"))[0]["data"] == {"linkColor": "#f00"}
    assert json.loads(archive.read("suppressions.json"))[0]["email"] == "no@example.org"
    messages = json.loads(archive.read("messages.json"))
    assert messages[0]["recipients"][0]["email"] == "r@example.org" and "html" not in messages[0]
    assert "private" not in archive.read("domains.json").decode().lower()

    actions = [e["action"] for e in user.get("/v1/account/audit-events").json()["items"]]
    assert {"export.requested", "export.completed", "export.downloaded"} <= set(actions)

    record = db.session.get(m.AccountExport, export["id"])
    record.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert user.get(f"/v1/account/exports/{export['id']}/download").status_code == 410


def test_members_cannot_export(make_user) -> None:  # type: ignore[no-untyped-def]
    from tests.test_accounts import invite_and_accept, team

    owner, member = make_user(), make_user()
    team(owner)
    invite_and_accept(owner, member)
    member.account_id = owner.account_id
    assert member.post("/v1/account/exports").status_code == 403


def test_account_deletion(make_user, resolver, worker, transport, storage, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import png_bytes

    user = make_user()
    setup = verified_sender(user, resolver)
    key = user.post("/v1/account/api-keys", json={"name": "k"}).json()["key"]
    message = send(user, setup["identity"]["email"], ["r@example.org"]).json()
    upload = user.post("/v1/assets/uploads", json={"filename": "a.png", "content_type": "image/png",
                                                    "size": 100}).json()
    storage.objects[storage.presigned[-1]["key"]] = (png_bytes(), "image/png")
    asset = user.post(f"/v1/assets/{upload['asset']['id']}/finalize").json()

    wrong = user.post("/v1/account/deletion", json={"confirm_name": "nope"})
    assert wrong.status_code == 422
    assert user.post("/v1/account/deletion", json={"confirm_name": "Personal"}).status_code == 202

    # Sessions and keys are revoked …
    assert user.get("/v1/auth/me").status_code == 401
    assert user.client.get("/v1/messages", headers={"Authorization": f"Bearer {key}"}).status_code == 401
    # … pending sends stopped, domains and senders disabled, assets removed.
    recipient = db.session.query(m.MessageRecipient).filter_by(message_id=message["id"]).one()
    assert recipient.status == "failed"
    worker.process_outbox()
    assert transport.sent == []
    domain = db.session.get(m.Domain, setup["domain"]["id"])
    assert domain.status == "disabled" and domain.disabled_at
    assert db.session.get(m.SenderIdentity, setup["identity"]["id"]).status == "disabled"
    assert db.session.get(m.Asset, asset["id"]).status == "deleted"
    assert not any("/public/" in k for k in storage.objects)
    actions = [e.action for e in db.session.query(m.AuditEvent).filter_by(account_id=user.account_id)]
    assert "account.deletion_requested" in actions and "account.assets_deleted" in actions

    # The domain can be registered again by someone else.
    other = make_user()
    assert other.post("/v1/domains", json={"name": setup["domain"]["name"]}).status_code == 201


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

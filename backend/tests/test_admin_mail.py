from datetime import timedelta
from email import message_from_bytes, policy

from app import models as m
from app.security import utcnow
from tests.test_admin import PASSWORD, PLAIN, admin_client, make_admin, post_mail, sign_in  # noqa: F401

PDF = b"%PDF-1.4 a small test document"


def upload(client, name: str = "terms.pdf", data: bytes = PDF, content_type: str = "application/pdf"):  # type: ignore[no-untyped-def]
    return client.post(f"/v1/admin/mail/attachments?filename={name}", content=data,
                       headers={"Content-Type": content_type})


def test_write_and_send_an_email_with_attachments(admin_client, make_user, worker, transport, db) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    customer = make_user()
    options = admin_client.get("/v1/admin/mail/options").json()
    assert options["from_addresses"][0] == "support@mailvender.localhost"

    pdf = upload(admin_client)
    assert pdf.status_code == 201 and pdf.json()["size"] == len(PDF)
    picture = upload(admin_client, "logo.png", b"\x89PNG fake", "image/png").json()
    assert upload(admin_client, "setup.exe", b"MZ").json()["error"]["code"] == "attachment_blocked"
    assert upload(admin_client, "empty.txt", b"").status_code == 422

    bad = admin_client.post("/v1/admin/mail", json={"from_address": "ceo@elsewhere.example", "to": ["nope"],
                                                    "subject": " ", "body": ""})
    fields = {f["field"] for f in bad.json()["error"]["fields"]}
    assert {"from_address", "to[0]", "to", "subject", "body"} <= fields

    sent = admin_client.post("/v1/admin/mail", json={
        "from_address": "support@mailvender.localhost", "to": [customer.email, "Partner@Other.example"],
        "cc": ["partner@other.example", "boss@other.example"], "bcc": ["records@other.example"],
        "subject": "Your   account", "body": "Hello,\n\nSee https://mailvender.com/help <b>now</b>.",
        "attachment_ids": [pdf.json()["id"], picture["id"]],
    })
    assert sent.status_code == 201, sent.text
    mail = sent.json()
    assert mail["mail"]["status"] == "queued" and mail["mail"]["subject"] == "Your account"
    assert mail["mail"]["cc"] == ["boss@other.example"]  # already in To
    assert len(mail["attachments"]) == 2
    # Attached files can't be removed or reused.
    assert admin_client.delete(f"/v1/admin/mail/attachments/{picture['id']}").status_code == 404

    worker.process_outbox()
    envelope_from, recipients, raw = transport.sent[-1]
    assert envelope_from == "support@mailvender.localhost"
    assert recipients == [customer.email, "partner@other.example", "boss@other.example", "records@other.example"]
    message = message_from_bytes(raw, policy=policy.default)
    assert message["From"] == "Mailvender <support@mailvender.localhost>"
    assert message["Bcc"] is None and "records@" not in raw.decode(errors="replace").split("\r\n\r\n")[0]
    html = message.get_body(preferencelist=("html",)).get_content()
    assert '<a href="https://mailvender.com/help"' in html and "&lt;b&gt;now&lt;/b&gt;" in html
    files = {part.get_filename(): part.get_payload(decode=True) for part in message.iter_attachments()}
    assert files == {"terms.pdf": PDF, "logo.png": b"\x89PNG fake"}

    listed = admin_client.get("/v1/admin/mail").json()["items"]
    assert listed[0]["status"] == "sent" and listed[0]["sent_at"]
    assert admin_client.get("/v1/admin/mail?q=partner@other").json()["items"]
    assert admin_client.get("/v1/admin/mail?q=nothing-like-this").json()["items"] == []
    download = admin_client.get(f"/v1/admin/mail/attachments/{pdf.json()['id']}")
    assert download.content == PDF and "attachment" in download.headers["content-disposition"]
    audit = db.session.query(m.AuditEvent).filter_by(action="admin.mail_sent").one()
    assert audit.data["recipients"] == 4 and audit.account_id is None


def test_reply_and_forward_from_the_inbox(admin_client, client, worker, transport) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    assert post_mail(client, PLAIN, "support@mailvender.localhost").status_code == 200
    message = admin_client.get("/v1/admin/inbox?mailbox=support").json()["items"][0]
    assert admin_client.get("/v1/admin/inbox/counts").json()["unread"]["support"] == 1

    copied = admin_client.post(f"/v1/admin/mail/attachments/from-inbox/{message['id']}")
    assert copied.status_code == 201
    assert [x["filename"] for x in copied.json()] == ["evidence_report.pdf"]  # forwarding keeps the files
    reply = admin_client.post("/v1/admin/mail", json={
        "from_address": "support@mailvender.localhost", "to": [message["from_address"]],
        "subject": "Re: " + message["subject"], "body": "Thanks!", "format": "plain",
        "reply_to_message_id": message["id"],
    }).json()
    assert reply["in_reply_to_id"] == message["id"]
    worker.process_outbox()
    sent = message_from_bytes(transport.sent[-1][2], policy=policy.default)
    original = admin_client.get(f"/v1/admin/inbox/{message['id']}").json()
    message_id = next(h["value"] for h in original["headers"] if h["name"] == "Message-ID")
    assert sent["In-Reply-To"] == message_id and sent["References"] == message_id
    assert sent.get_body(preferencelist=("html",)) is None  # plain: text only


def test_preview_and_failures(admin_client, worker, transport, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    preview = admin_client.post("/v1/admin/mail/preview", json={"subject": "Hi", "body": "<script>x</script>"})
    assert "&lt;script&gt;" in preview.text and "sandbox" in preview.headers["content-security-policy"]

    from app.services.mta import PermanentDeliveryError

    def refuse(*_args):  # type: ignore[no-untyped-def]
        raise PermanentDeliveryError("550 no such user", 550)

    monkeypatch.setattr(transport, "send", refuse)
    admin_client.post("/v1/admin/mail", json={"from_address": "support@mailvender.localhost",
                                              "to": ["x@other.example"], "subject": "Hi", "body": "Hello"})
    worker.process_outbox()
    failed = admin_client.get("/v1/admin/mail").json()["items"][0]
    assert failed["status"] == "failed" and "550" in failed["error"]


def test_attachment_limits_and_cleanup(admin_client, worker, storage, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.config import get_settings

    sign_in(admin_client)
    monkeypatch.setattr(get_settings(), "admin_mail_max_attachment_bytes", 10)
    monkeypatch.setattr(get_settings(), "admin_mail_max_total_bytes", 15)
    assert upload(admin_client, data=b"x" * 11).status_code == 413
    first, second = upload(admin_client, data=b"x" * 10).json(), upload(admin_client, data=b"y" * 10).json()
    too_big = admin_client.post("/v1/admin/mail", json={"from_address": "support@mailvender.localhost",
                                                        "to": ["x@other.example"], "subject": "Hi", "body": "Hello",
                                                        "attachment_ids": [first["id"], second["id"]]})
    assert too_big.json()["error"]["fields"][0]["code"] == "too_large"
    assert admin_client.delete(f"/v1/admin/mail/attachments/{second['id']}").status_code == 204

    # Uploaded but never sent: removed (file too) after a day.
    db.session.query(m.AdminMailAttachment).update({"created_at": utcnow() - timedelta(days=2)})
    db.commit()
    worker.run_job(next(j for j in worker.jobs if j.name == "retention"))
    assert db.session.query(m.AdminMailAttachment).count() == 0
    assert not [k for k in storage.objects if "/admin-mail/" in k]


# ---- The admin's own login, users, audit filter ----


def test_recovery_codes_and_signing_out_elsewhere(admin_client, db) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    assert admin_client.post("/v1/admin/auth/recovery-codes", json={"password": "wrong"}).status_code == 422
    codes = admin_client.post("/v1/admin/auth/recovery-codes", json={"password": PASSWORD}).json()["recovery_codes"]
    assert len(codes) == 10
    assert admin_client.get("/v1/admin/auth/me").json()["recovery_codes_left"] == 10
    assert admin_client.post("/v1/admin/auth/sign-out-others").json() == {"sessions": 0}
    assert [x["email"] for x in admin_client.get("/v1/admin/admins").json()] == ["boss@example.com"]


def test_sign_a_user_out_and_send_a_password_reset(admin_client, make_user, db) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    user = make_user()
    assert admin_client.post(f"/v1/admin/users/{user.user_id}/sign-out", json={"reason": "lost laptop"}).json()[
        "active_sessions"] == 0
    assert user.get("/v1/auth/me").status_code == 401
    assert admin_client.post(f"/v1/admin/users/{user.user_id}/password-reset").status_code == 200
    emails = [e.payload["template"] for e in db.session.query(m.OutboxEvent).filter_by(kind="system_email")
              if e.payload["to"] == user.email]
    assert emails[-1] == "password_reset"
    actions = {e["action"] for e in admin_client.get("/v1/admin/audit").json()["items"]}
    assert {"user.signed_out_everywhere", "user.password_reset_sent"} <= actions


def test_accounts_list_counts_the_last_24_hours(admin_client, make_user, resolver, db) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import send, verified_sender

    sign_in(admin_client)
    user = make_user()
    setup = verified_sender(user, resolver)
    send(user, setup["identity"]["email"], ["r@example.org"])
    db.session.query(m.MessageRecipient).update({"created_at": utcnow() - timedelta(hours=20)})
    db.commit()
    row = next(r for r in admin_client.get("/v1/admin/accounts").json()["items"]
               if r["account"]["id"] == user.account_id)
    assert row["sent_today"] == 1  # same window as the daily limit, even across midnight


def test_ready_admin_needed_for_mail(admin_client) -> None:  # type: ignore[no-untyped-def]
    make_admin("new@example.com", ready=False)
    login = admin_client.post("/v1/admin/auth/login", json={"email": "new@example.com", "password": PASSWORD})
    assert login.status_code == 200
    assert admin_client.get("/v1/admin/mail/options").json()["error"]["code"] == "password_change_required"

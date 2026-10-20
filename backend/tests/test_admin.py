import gzip
import hashlib
import hmac
import io
import zipfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.api import routes_admin
from app.cli import main as cli_main
from app.config import get_settings
from app.main import app
from app.security import encrypt_secret, hash_password, new_totp_secret, totp_code, totp_step, utcnow
from app.services.admin_auth import AdminAuthService
from tests.conftest import CSRF, sql_uow
from tests.test_feedback import ARF, delivered
from tests.test_health import add_volume

SECRET = {"X-Mailvender-Admin-Secret": "admin-proxy-secret-for-tests-0123456789"}
ADMIN_CSRF = {"X-Requested-With": "mailvender-admin"}
PASSWORD = "plum tractor violet harbour"
INBOUND_SECRET = b"inbound-mail-secret-for-tests-0123456789"


@pytest.fixture
def admin_client(storage, resolver):  # type: ignore[no-untyped-def]
    """A browser behind the admin app's proxy: every request carries the admin key and a client IP."""
    from app.api import deps

    app.dependency_overrides[deps.get_storage] = lambda: storage
    app.dependency_overrides[deps.get_resolver] = lambda: resolver
    app.dependency_overrides[routes_admin.get_port25_probe] = lambda: (lambda: True)
    with TestClient(app, base_url="https://testserver",
                    headers={**SECRET, **ADMIN_CSRF, "X-Mailvender-Client-Ip": "203.0.113.7"}) as client:
        yield client
    app.dependency_overrides.clear()


def make_admin(email: str = "boss@example.com", *, ready: bool = True) -> str:
    """Creates an admin; when `ready`, past the first-sign-in steps. Returns the TOTP secret."""
    secret = new_totp_secret()
    with sql_uow() as uow:
        uow.admins.add(m.AdminUser(
            email=email, password_hash=hash_password(PASSWORD), must_change_password=not ready,
            totp_secret_encrypted=encrypt_secret(secret) if ready else None,
            totp_enabled_at=utcnow() if ready else None, recovery_code_hashes=[],
        ))
        uow.commit()
    return secret


def sign_in(client: TestClient, email: str = "boss@example.com") -> None:
    secret = make_admin(email)
    response = client.post("/v1/admin/auth/login",
                           json={"email": email, "password": PASSWORD, "code": totp_code(secret, totp_step())})
    assert response.status_code == 200, response.text


def signed(body: bytes) -> dict[str, str]:
    return {"X-Mailvender-Signature": "sha256=" + hmac.new(INBOUND_SECRET, body, hashlib.sha256).hexdigest(),
            "Content-Type": "message/rfc822"}


def post_mail(client: TestClient, body: bytes, to: str = "postmaster@mailvender.localhost"):  # type: ignore[no-untyped-def]
    return client.post(f"/internal/inbound-mail?to={to}", content=body, headers=signed(body))


# ---- The proxy key ----


def test_admin_routes_are_invisible_without_the_admin_key(client) -> None:  # type: ignore[no-untyped-def]
    for headers in ({}, {"X-Mailvender-Admin-Secret": "wrong"}, {"X-Mailvender-Proxy-Secret": "anything"}):
        assert client.get("/v1/admin/auth/me", headers=headers).status_code == 404
        # The wrong method or a bad body would normally say 405/422 – here it's the same 404.
        assert client.get("/v1/admin/auth/login", headers=headers).status_code == 404
        assert client.post("/v1/admin/auth/login", content=b"{", headers=headers).status_code == 404
    assert client.get("/v1/admin/auth/me", headers=SECRET).status_code == 401
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/v1/admin") or p == "/internal/inbound-mail"]


def test_a_blank_admin_key_turns_the_admin_api_off(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "admin_proxy_secret", "")
    assert client.get("/v1/admin/auth/me", headers=SECRET).status_code == 404


def test_admin_openapi_is_separate() -> None:
    from app.main import admin_openapi

    paths = admin_openapi(app)["paths"]
    assert "/v1/admin/auth/login" in paths and "/v1/messages" not in paths


# ---- Sign-in ----


def test_first_sign_in_forces_a_new_password_then_two_factor(admin_client, db) -> None:  # type: ignore[no-untyped-def]
    admin, temporary = AdminAuthService(db).create("New.Admin@Example.com")
    response = admin_client.post("/v1/admin/auth/login", json={"email": "new.admin@example.com",
                                                                "password": temporary})
    assert response.status_code == 200, response.text
    assert response.json()["admin"]["next_step"] == "change_password"
    cookie = response.headers["set-cookie"]
    assert "__Host-mv_admin_session=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Secure" in cookie

    assert admin_client.get("/v1/admin/overview").json()["error"]["code"] == "password_change_required"
    assert admin_client.post("/v1/admin/auth/two-factor/setup").json()["error"]["code"] == "password_change_required"

    def change(new: str, current: str = temporary):  # type: ignore[no-untyped-def]
        return admin_client.post("/v1/admin/auth/password", json={"current_password": current, "new_password": new})

    assert change("short").json()["error"]["fields"][0]["code"] == "password_length"
    assert change("passwordpassword").json()["error"]["fields"][0]["code"] == "password_common"
    assert change("new.admin likes long walks").json()["error"]["fields"][0]["code"] == "password_contains_email"
    assert change(PASSWORD, current="wrong password").json()["error"]["fields"][0]["field"] == "current_password"
    assert change(PASSWORD).json()["next_step"] == "setup_two_factor"
    assert admin_client.get("/v1/admin/users").json()["error"]["code"] == "two_factor_setup_required"

    setup = admin_client.post("/v1/admin/auth/two-factor/setup").json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/Mailvender%20Admin:new.admin%40example.com?")
    bad = admin_client.post("/v1/admin/auth/two-factor/confirm", json={"code": "000000"})
    assert bad.status_code == 422
    step = totp_step()
    codes = admin_client.post("/v1/admin/auth/two-factor/confirm",
                              json={"code": totp_code(setup["secret"], step)}).json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    me = admin_client.get("/v1/admin/auth/me").json()
    assert me["next_step"] is None and me["two_factor_enabled"] and me["recovery_codes_left"] == 10
    assert admin_client.get("/v1/admin/overview").status_code == 200

    assert admin_client.post("/v1/admin/auth/logout").status_code == 204
    assert admin_client.get("/v1/admin/auth/me").status_code == 401

    login = {"email": "new.admin@example.com", "password": PASSWORD}
    assert admin_client.post("/v1/admin/auth/login", json=login).json()["error"]["code"] == "totp_required"
    # The code used to confirm the setup can't be used again.
    reused = admin_client.post("/v1/admin/auth/login", json={**login, "code": totp_code(setup["secret"], step)})
    assert reused.json()["error"]["code"] == "invalid_credentials"
    assert admin_client.post("/v1/admin/auth/login", json={**login, "code": codes[0]}).status_code == 200
    assert admin_client.get("/v1/admin/auth/me").json()["recovery_codes_left"] == 9
    again = admin_client.post("/v1/admin/auth/login", json={**login, "code": codes[0].upper()})
    assert again.json()["error"]["code"] == "invalid_credentials"  # each recovery code works once

    actions = {e.action for e in db.session.query(m.AuditEvent).filter_by(actor_id=admin.id)}
    assert {"admin.signed_in", "admin.password_changed", "admin.two_factor_enabled",
            "admin.recovery_code_used"} <= actions
    signed_in = db.session.query(m.AuditEvent).filter_by(action="admin.signed_in").first()
    assert signed_in.data["ip"] == "203.0.113.7" and signed_in.account_id is None


def test_wrong_details_lock_the_admin_out(admin_client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_admin_login_ip", "100/900")
    monkeypatch.setattr(settings, "rate_limit_admin_login_email", "100/900")
    secret = make_admin()
    wrong = {"email": "boss@example.com", "password": "not the password"}
    for _ in range(10):
        assert admin_client.post("/v1/admin/auth/login", json=wrong).json()["error"]["code"] == "invalid_credentials"
    right = {"email": "boss@example.com", "password": PASSWORD, "code": totp_code(secret, totp_step())}
    assert admin_client.post("/v1/admin/auth/login", json=right).json()["error"]["code"] == "invalid_credentials"
    unknown = {"email": "nobody@example.com", "password": PASSWORD}
    assert admin_client.post("/v1/admin/auth/login", json=unknown).json()["error"]["code"] == "invalid_credentials"
    assert db.session.query(m.AuditEvent).filter_by(action="admin.locked").count() == 1


def test_admin_login_is_rate_limited(admin_client) -> None:  # type: ignore[no-untyped-def]
    make_admin()
    wrong = {"email": "boss@example.com", "password": "nope nope nope"}
    codes = [admin_client.post("/v1/admin/auth/login", json=wrong).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429


def test_admin_sessions_end_when_idle_and_need_the_admin_csrf_header(admin_client, db) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    no_csrf = admin_client.post("/v1/admin/issues/state", headers={"X-Requested-With": "mailvender"},
                                json={"key": "suspended:x", "status": "open"})
    assert no_csrf.json()["error"]["code"] == "csrf_failed"
    db.session.query(m.AdminSession).update({"last_seen_at": utcnow() - timedelta(minutes=31)})
    db.commit()
    assert admin_client.get("/v1/admin/auth/me").json()["error"]["code"] == "admin_session_invalid"


def test_customer_and_admin_sessions_never_mix(admin_client, make_user) -> None:  # type: ignore[no-untyped-def]
    customer = make_user()
    # A customer's own session, even with the admin key, isn't an admin session.
    assert customer.client.get("/v1/admin/auth/me", headers=SECRET).status_code == 401
    sign_in(admin_client)
    assert admin_client.get("/v1/auth/me", headers=CSRF).status_code == 401


# ---- Customers ----


def test_block_and_unblock_a_user(admin_client, make_user, db) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    customer = make_user()
    key = customer.post("/v1/account/api-keys", json={"name": "ci"}).json()["key"]
    path = f"/v1/admin/users/{customer.user_id}"
    assert admin_client.post(f"{path}/block", json={"reason": "x"}).status_code == 422  # a reason is required
    blocked = admin_client.post(f"{path}/block", json={"reason": "Phishing campaign"}).json()
    assert blocked["user"]["status"] == "blocked"
    assert blocked["accounts"][0]["status"] == "suspended"
    assert customer.get("/v1/auth/me").status_code == 401  # signed out at once
    login = customer.client.post("/v1/auth/login", json={"email": customer.email, "password": "correct horse battery"},
                                 headers=CSRF)
    assert login.json()["error"]["code"] == "user_disabled"
    api = customer.client.get("/v1/messages", headers={"Authorization": f"Bearer {key}"})
    assert api.status_code == 401
    assert admin_client.post(f"{path}/block", json={"reason": "again"}).json()["error"]["code"] == "already_blocked"

    unblocked = admin_client.post(f"{path}/unblock", json={"reason": "Appeal accepted"}).json()
    assert unblocked["user"]["status"] == "active"
    assert unblocked["accounts"][0]["status"] == "suspended"  # reinstating sending is a separate decision

    event = db.session.query(m.AuditEvent).filter_by(action="user.blocked").one()
    assert event.actor_type == "operator" and event.reason == "Phishing campaign"
    assert event.data["ip"] == "203.0.113.7" and event.account_id is None


def test_users_accounts_and_account_decisions(admin_client, make_user, db) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    alice, bob = make_user("alice@example.com"), make_user("bob@example.com")
    add_volume(db, bob.account_id, accepted=40, hard_bounces=2, complaints=1)

    users = admin_client.get("/v1/admin/users?q=ALICE").json()["items"]
    assert [u["email"] for u in users] == ["alice@example.com"] and users[0]["status"] == "active"
    assert admin_client.get("/v1/admin/users?status=blocked").json()["items"] == []

    rows = admin_client.get("/v1/admin/accounts?q=bob@").json()["items"]
    assert len(rows) == 1 and rows[0]["owner_email"] == "bob@example.com"
    assert rows[0]["metrics"]["accepted"] == 40 and rows[0]["metrics"]["complaints"] == 1

    path = f"/v1/admin/accounts/{bob.account_id}"
    detail = admin_client.get(path).json()
    assert detail["owner_email"] == "bob@example.com" and detail["members"][0]["role"] == "owner"
    assert detail["recent_events"][0]["email"].startswith("r") and "***@" in detail["recent_events"][0]["email"]
    assert detail["status"] == "active"

    suspended = admin_client.post(f"{path}/suspend", json={"reason": "Complaints"}).json()
    assert suspended["status"] == "suspended"
    limits = admin_client.put(f"{path}/limits", json={"hourly_recipient_limit": 5, "daily_recipient_limit": 20,
                                                      "reason": "New sender"}).json()
    assert limits["account"]["daily_recipient_limit"] == 20
    assert admin_client.post(f"{path}/reinstate", json={"reason": "Cleaned list"}).json()["status"] == "active"
    assert admin_client.post(f"{path}/reviews", json={"reason": "Looked fine"}).json()["latest_decision"]["action"] \
        == "account.reviewed"

    # Customers see the decision in their audit log, but not the admin-only rows.
    actions = [e["action"] for e in bob.get("/v1/account/audit-events?limit=100").json()["items"]]
    assert "account.suspended" in actions and not [a for a in actions if a.startswith("admin.")]
    audit = admin_client.get("/v1/admin/audit?limit=100").json()["items"]
    assert {"admin.viewed_account", "admin.account_suspend", "account.suspended"} <= {e["action"] for e in audit}
    assert admin_client.get(f"/v1/admin/users/{alice.user_id}").json()["accounts"][0]["role"] == "owner"


def test_needs_help_lists_problems_with_fixes(admin_client, make_user, client, db) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    client.post("/v1/auth/signup", json={"email": "stuck@example.com", "password": "correct horse battery"},
                headers=CSRF)
    db.session.query(m.User).filter_by(email="stuck@example.com").update({"created_at": utcnow() - timedelta(days=3)})
    customer = make_user("owner@example.com")
    admin_client.post(f"/v1/admin/accounts/{customer.account_id}/suspend", json={"reason": "Spam trap hits"})
    db.commit()

    result = admin_client.get("/v1/admin/issues").json()
    by_type = {i["type"]: i for i in result["items"]}
    assert by_type["suspended"]["severity"] == "high" and "Spam trap hits" in by_type["suspended"]["problem"]
    assert by_type["suspended"]["action"] == "reinstate"
    stuck = by_type["unverified_signup"]
    assert stuck["who"] == "stuck@example.com" and "spam folder" in stuck["how_to_fix"]
    assert result["items"][0]["type"] == "suspended"  # worst first
    counts = {t["type"]: t["count"] for t in result["types"]}
    assert counts["unverified_signup"] == 1 and counts["suspended"] == 1

    resent = admin_client.post(f"/v1/admin/users/{stuck['user_id']}/resend-verification")
    assert resent.status_code == 200
    assert admin_client.post("/v1/admin/issues/state", json={"key": stuck["key"], "status": "contacted",
                                                               "note": "Emailed them"}).status_code == 204
    contacted = next(i for i in admin_client.get("/v1/admin/issues").json()["items"] if i["key"] == stuck["key"])
    assert contacted["state"] == "contacted" and contacted["note"] == "Emailed them"
    admin_client.post("/v1/admin/issues/state", json={"key": stuck["key"], "status": "resolved"})
    assert stuck["key"] not in [i["key"] for i in admin_client.get("/v1/admin/issues").json()["items"]]
    db.session.query(m.AdminIssueState).update({"updated_at": utcnow() - timedelta(days=2)})
    db.commit()
    back = next(i for i in admin_client.get("/v1/admin/issues").json()["items"] if i["key"] == stuck["key"])
    assert back["reopened"] and back["state"] == "open"


def test_overview_deliverability_and_system(admin_client, make_user, db, storage) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    customer = make_user()
    add_volume(db, customer.account_id, accepted=50, hard_bounces=1, complaints=1)
    customer.get("/v1/auth/me")
    prefix = get_settings().aws_s3_prefix
    storage.put(f"{prefix}backups/db/{utcnow():%Y/%m/%d}/mailvender-x.dump.age", b"age", "application/octet-stream")

    overview = admin_client.get("/v1/admin/overview?days=7").json()
    cards = {c["key"]: c for c in overview["cards"]}
    assert cards["delivered"]["today"] == 50 and cards["complaints"]["today"] == 1
    assert cards["signups"]["today"] >= 1 and cards["active_users"]["today"] >= 1
    assert len(overview["series"]) == 7 and overview["series"][-1]["delivered"] == 50
    assert overview["series"][-1]["complaint_rate"] == pytest.approx(0.02)
    assert overview["checks"] and overview["checks"][0]["status"] in ("problem", "warning")

    deliverability = admin_client.get("/v1/admin/deliverability").json()
    today = deliverability["windows"][0]
    assert today["complaint_rate"] == pytest.approx(0.02) and today["spam_level"] == "bad"
    assert deliverability["by_provider"][0]["provider"] == "Other"
    assert deliverability["worst_accounts"][0]["account_id"] == customer.account_id

    system = admin_client.get("/v1/admin/system").json()
    assert system["port25_open"] is True
    database = next(b for b in system["backups"] if b["label"] == "Database")
    assert database["key"].endswith("mailvender-x.dump.age")
    assert admin_client.get("/v1/admin/overview?days=400").status_code == 422


# ---- Role inboxes ----

PLAIN = b"""From: Jane Doe <jane@isp.example>
To: postmaster@mailvender.localhost
Subject: Your server sent us spam
Message-ID: <abc123@isp.example>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="m"

--m
Content-Type: multipart/alternative; boundary="a"

--a
Content-Type: text/plain

Please look into this.
--a
Content-Type: text/html

<p>Please <b>look</b> into this.<img src="https://tracker.example/pixel.gif"></p><script>alert(1)</script>
--a--
--m
Content-Type: application/pdf
Content-Disposition: attachment; filename="../../evidence report.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--m--
"""


def dmarc_xml(source_ip: str = "2.29.100.188", dkim: str = "pass") -> bytes:
    end = int(utcnow().timestamp())
    return f"""<?xml version="1.0"?>
<feedback>
  <report_metadata><org_name>google.com</org_name><report_id>r1</report_id>
    <date_range><begin>{end - 86400}</begin><end>{end}</end></date_range></report_metadata>
  <policy_published><domain>mailvender.com</domain><p>none</p></policy_published>
  <record><row><source_ip>{source_ip}</source_ip><count>12</count>
    <policy_evaluated><disposition>none</disposition><dkim>{dkim}</dkim><spf>pass</spf></policy_evaluated></row></record>
  <record><row><source_ip>198.51.100.9</source_ip><count>3</count>
    <policy_evaluated><disposition>none</disposition><dkim>fail</dkim><spf>fail</spf></policy_evaluated></row></record>
</feedback>""".encode()


def report_mail(filename: str, content_type: str, data: bytes, message_id: str = "<rep1@google.com>") -> bytes:
    import base64

    encoded = base64.encodebytes(data).decode()
    return f"""From: noreply-dmarc-support@google.com
To: dmarc@mailvender.localhost
Subject: Report domain: mailvender.com
Message-ID: {message_id}
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="d"

--d
Content-Type: text/plain

DMARC report attached.
--d
Content-Type: {content_type}
Content-Disposition: attachment; filename="{filename}"
Content-Transfer-Encoding: base64

{encoded}
--d--
""".encode()


def test_inbound_mail_is_signed_and_limited_to_the_role_addresses(client) -> None:  # type: ignore[no-untyped-def]
    assert client.post("/internal/inbound-mail?to=postmaster@mailvender.localhost", content=PLAIN,
                       headers={"X-Mailvender-Signature": "sha256=bad"}).status_code == 401
    assert post_mail(client, PLAIN, "ceo@mailvender.localhost").json()["error"]["code"] == "unknown_mailbox"
    assert post_mail(client, PLAIN, "postmaster@evil.example").status_code == 422
    first = post_mail(client, PLAIN).json()
    assert first["created"] and first["kind"] == "plain"
    assert post_mail(client, PLAIN).json() == {**first, "created": False}  # delivered twice, stored once


def test_reading_the_inbox(admin_client, client, storage, db) -> None:  # type: ignore[no-untyped-def]
    sign_in(admin_client)
    post_mail(client, PLAIN)
    counts = admin_client.get("/v1/admin/inbox/counts").json()["unread"]
    assert counts == {"postmaster": 1, "abuse": 0, "dmarc": 0}
    listing = admin_client.get("/v1/admin/inbox?mailbox=postmaster&q=spam").json()["items"]
    assert len(listing) == 1 and listing[0]["from_name"] == "Jane Doe" and listing[0]["has_attachments"]
    message_id = listing[0]["id"]
    assert admin_client.get("/v1/admin/inbox?mailbox=postmaster&q=nothing-like-this").json()["items"] == []

    detail = admin_client.get(f"/v1/admin/inbox/{message_id}").json()
    assert detail["text"].strip() == "Please look into this." and detail["has_html"]
    assert ("Subject", "Your server sent us spam") in [(h["name"], h["value"]) for h in detail["headers"]]
    assert detail["attachments"] == [{"index": 0, "filename": "../../evidence report.pdf",
                                      "content_type": "application/pdf", "size": 9}]
    assert detail["message"]["read_at"] is not None  # opening marks it read
    assert db.session.query(m.AuditEvent).filter_by(action="admin.viewed_inbox_message").count() == 1

    body = admin_client.get(f"/v1/admin/inbox/{message_id}/body")
    csp = body.headers["content-security-policy"]
    assert "img-src data:;" in csp and "sandbox" in csp and "script" not in body.text
    assert "default-src 'none'" in csp and body.headers["x-frame-options"] == "SAMEORIGIN"
    with_images = admin_client.get(f"/v1/admin/inbox/{message_id}/body?images=true")
    assert "img-src data: https: http:" in with_images.headers["content-security-policy"]

    attachment = admin_client.get(f"/v1/admin/inbox/{message_id}/attachments/0")
    assert attachment.headers["content-disposition"] == 'attachment; filename="evidence_report.pdf"'
    assert attachment.headers["content-type"] == "application/octet-stream" and attachment.content == b"%PDF-1.4\n"
    assert admin_client.get(f"/v1/admin/inbox/{message_id}/attachments/5").status_code == 404
    assert admin_client.get(f"/v1/admin/inbox/{message_id}/raw").content == PLAIN

    archived = admin_client.post(f"/v1/admin/inbox/{message_id}", json={"archived": True, "read": False}).json()
    assert archived["archived_at"] and archived["read_at"] is None
    assert admin_client.get("/v1/admin/inbox?mailbox=postmaster").json()["items"] == []
    assert len(admin_client.get("/v1/admin/inbox?mailbox=postmaster&state=archived").json()["items"]) == 1


def test_complaints_at_abuse_feed_the_complaint_pipeline(client, make_user, resolver, worker, transport, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    _, _, rid, _ = delivered(user, resolver, worker, transport)
    body = ARF.format(rid=rid).encode()
    result = post_mail(client, body, "abuse@mailvender.localhost").json()
    assert result["kind"] == "complaint_report"
    stored = db.session.query(m.InboundMessage).one()
    assert stored.summary == {"correlated": True, "suppressed": True}
    suppression = db.session.query(m.Suppression).one()
    assert (suppression.reason, suppression.source) == ("complaint", "fbl:abuse-mailbox")


def test_dmarc_reports_are_parsed(admin_client, client, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "known_sending_ips", "2.29.100.188")
    sign_in(admin_client)
    compressed = gzip.compress(dmarc_xml())
    result = post_mail(client, report_mail("google.com!mailvender.com!1!2.xml.gz", "application/gzip", compressed),
                       "dmarc@mailvender.localhost").json()
    assert result["kind"] == "dmarc_report"
    stored = db.session.query(m.InboundMessage).one()
    assert stored.summary["messages"] == 15 and stored.summary["passed"] == 12
    assert db.session.query(m.DmarcReportRow).count() == 2

    detail = admin_client.get(f"/v1/admin/inbox/{stored.id}").json()
    assert {r["source_ip"] for r in detail["dmarc_rows"]} == {"2.29.100.188", "198.51.100.9"}
    deliverability = admin_client.get("/v1/admin/deliverability?days=90").json()
    reporter = deliverability["dmarc_reporters"][0]
    assert reporter["reporter"] == "google.com" and reporter["messages"] == 15 and reporter["dmarc_pass"] == 12
    sources = {s["source_ip"]: s for s in deliverability["dmarc_sources"]}
    assert sources["2.29.100.188"]["ours"] and not sources["2.29.100.188"]["suspicious"]
    assert sources["198.51.100.9"]["suspicious"]


def test_hostile_dmarc_attachments_are_refused(client, db) -> None:  # type: ignore[no-untyped-def]
    xxe = b"""<?xml version="1.0"?><!DOCTYPE feedback [<!ENTITY x SYSTEM "file:///etc/passwd">]>
<feedback><report_metadata><org_name>&x;</org_name></report_metadata></feedback>"""
    post_mail(client, report_mail("r.xml", "text/xml", xxe, "<xxe@x>"), "dmarc@mailvender.localhost")

    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.xml", b"<feedback>" + b" " * (21 * 1024 * 1024) + b"</feedback>")
    post_mail(client, report_mail("r.zip", "application/zip", bomb.getvalue(), "<bomb@x>"),
              "dmarc@mailvender.localhost")

    many = io.BytesIO()
    with zipfile.ZipFile(many, "w") as archive:
        for index in range(6):
            archive.writestr(f"{index}.xml", dmarc_xml())
    post_mail(client, report_mail("r.zip", "application/zip", many.getvalue(), "<many@x>"),
              "dmarc@mailvender.localhost")

    errors = sorted(row.summary.get("error", "") for row in db.session.query(m.InboundMessage))
    assert errors == ["Reports with DOCTYPE or ENTITY declarations are refused.", "The report is too large.",
                      "The zip file holds too many files."]
    assert db.session.query(m.DmarcReportRow).count() == 0


def test_old_inbox_mail_is_deleted_by_retention(client, worker, storage, db) -> None:  # type: ignore[no-untyped-def]
    post_mail(client, PLAIN)
    stored = db.session.query(m.InboundMessage).one()
    key = stored.object_key
    assert key in storage.objects and key.startswith("mailvender/inbox/postmaster/")
    db.session.query(m.InboundMessage).update({"created_at": utcnow() - timedelta(days=181)})
    db.commit()
    worker.run_job(next(j for j in worker.jobs if j.name == "retention"))
    assert db.session.query(m.InboundMessage).count() == 0 and key not in storage.objects


# ---- CLI ----


def test_cli_creates_resets_and_disables_admins(capsys) -> None:  # type: ignore[no-untyped-def]
    assert cli_main(["create-admin", "ops@example.com"]) == 0
    assert "Temporary password (shown once)" in capsys.readouterr().out
    assert cli_main(["create-admin", "ops@example.com"]) == 1
    assert cli_main(["reset-admin", "ops@example.com"]) == 0
    assert cli_main(["list-admins"]) == 0
    assert "setup pending" in capsys.readouterr().out
    assert cli_main(["disable-admin", "ops@example.com"]) == 0
    assert cli_main(["disable-admin", "nobody@example.com"]) == 1


def test_only_one_active_admin(capsys) -> None:  # type: ignore[no-untyped-def]
    assert cli_main(["create-admin", "first@example.com"]) == 0
    assert cli_main(["create-admin", "second@example.com"]) == 1
    assert "Only 1 active admin allowed" in capsys.readouterr().err
    # Disabling makes room; the disabled one can't come back while another is active.
    assert cli_main(["disable-admin", "first@example.com"]) == 0
    assert cli_main(["create-admin", "second@example.com"]) == 0
    assert cli_main(["reset-admin", "first@example.com"]) == 1
    assert cli_main(["reset-admin", "second@example.com"]) == 0

"""Blocklists, link checks, alerts, breached passwords and customer two-factor sign-in."""

import hashlib

import httpx

from app import models as m
from app.config import get_settings
from app.security import totp_code, totp_step
from app.services.alerts import AlertService
from app.services.reputation import base_domain, link_domains
from tests.conftest import CSRF, PASSWORD, send, sql_uow, verified_sender
from tests.test_admin import admin_client, sign_in  # noqa: F401


# ---- Blocklists ----


def test_our_ip_on_a_blocklist_is_a_problem(admin_client, worker, resolver, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "known_sending_ips", "2.29.100.188")
    resolver.a_records["188.100.29.2.zen.spamhaus.org"] = ["127.0.0.2"]
    resolver.a_records["188.100.29.2.bl.spamcop.net"] = ["127.255.255.254"]  # refused: unknown, not listed
    worker.run_job(next(j for j in worker.jobs if j.name == "reputation"))
    sign_in(admin_client)
    checks = admin_client.get("/v1/admin/system").json()["checks"]
    listed = [c for c in checks if "blocklist" in c["title"]]
    assert listed == [{"status": "problem", "title": "2.29.100.188 is on the zen.spamhaus.org blocklist",
                       "detail": listed[0]["detail"]}]
    assert "check.spamhaus.org" in listed[0]["detail"]

    resolver.a_records.clear()
    worker.run_job(next(j for j in worker.jobs if j.name == "reputation"))
    assert any(c["title"] == "Not on any blocklist" for c in admin_client.get("/v1/admin/system").json()["checks"])


def test_links_to_blocklisted_domains_are_refused(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    resolver.a_records["phish-example.com.dbl.spamhaus.org"] = ["127.0.1.4"]
    refused = send(user, setup["identity"]["email"], ["r@example.org"],
                   html='<p>Hi <a href="https://login.phish-example.com/x">verify</a></p>')
    assert refused.status_code == 422
    assert refused.json()["error"]["fields"][0]["code"] == "blocklisted_link"
    ok = send(user, setup["identity"]["email"], ["r@example.org"], html='<a href="https://fine.example.org">x</a>')
    assert ok.status_code == 202, ok.text


def test_link_domains() -> None:
    html = '<a href="https://a.b.example.co.uk/x?y">1</a> <a href="http://Example.COM.">2</a> https://1.2.3.4/x {{unsubscribe_url}}'
    assert link_domains(html, "see https://shop.example.com/sale") == ["example.co.uk", "example.com"]
    assert base_domain("mail.google.com") == "google.com"


# ---- Alerts ----


def test_alerts_are_sent_once_and_when_fixed(storage, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "alert_emails", "ops@example.com")
    monkeypatch.setattr(get_settings(), "alert_webhook_url", "https://hooks.example.com/x")
    posts: list[dict] = []  # type: ignore[type-arg]
    port = {"open": False}

    def run() -> int:
        with sql_uow() as uow:
            return AlertService(uow, storage, lambda: port["open"], lambda url, body: posts.append(body)).run()

    assert run() == 2  # one email + the webhook
    assert "Outbound port 25 is blocked" in posts[0]["text"]
    assert run() == 0  # nothing changed: no repeat
    port["open"] = True
    assert run() == 2
    assert "Fixed:" in posts[-1]["text"] and "port 25" in posts[-1]["text"]
    templates = [e.payload["template"] for e in db.session.query(m.OutboxEvent).filter_by(kind="system_email")
                 if e.payload["to"] == "ops@example.com"]
    assert templates == ["alert", "alert"]


# ---- Breached passwords ----


def test_breached_passwords_are_refused(client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "pwned_passwords_check", True)
    leaked = "password1234567"
    suffix = hashlib.sha1(leaked.encode()).hexdigest().upper()[5:]  # noqa: S324

    def fake_get(url: str, **_kwargs):  # type: ignore[no-untyped-def]
        assert url.endswith(hashlib.sha1(leaked.encode()).hexdigest().upper()[:5]) or "range/" in url  # noqa: S324
        return httpx.Response(200, text=f"0000000000000000000000000000000000A:0\r\n{suffix}:4242\r\n",
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    response = client.post("/v1/auth/signup", json={"email": "new@example.com", "password": leaked}, headers=CSRF)
    assert response.json()["error"]["fields"][0]["code"] == "password_breached"

    def offline(url: str, **_kwargs):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(httpx, "get", offline)  # the check fails open
    assert client.post("/v1/auth/signup", json={"email": "new@example.com", "password": leaked},
                       headers=CSRF).status_code == 202


# ---- Customer two-factor sign-in ----


def test_customer_two_factor_sign_in(make_user, admin_client, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    secret = user.post("/v1/auth/two-factor/setup").json()["secret"]
    assert user.post("/v1/auth/two-factor/confirm", json={"code": "000000"}).status_code == 422
    codes = user.post("/v1/auth/two-factor/confirm", json={"code": totp_code(secret, totp_step())}).json()[
        "recovery_codes"]
    assert len(codes) == 10
    me = user.get("/v1/auth/me").json()["user"]
    assert me["two_factor_enabled"] and me["recovery_codes_left"] == 10

    def login(**extra):  # type: ignore[no-untyped-def]
        return user.client.post("/v1/auth/login", json={"email": user.email, "password": PASSWORD, **extra},
                                headers=CSRF)

    assert login().json()["error"]["code"] == "totp_required"
    assert login(code="123456").json()["error"]["code"] == "invalid_code"
    assert login(code=totp_code(secret, totp_step() + 1)).status_code == 200
    assert login(code=codes[0]).status_code == 200  # a recovery code, once
    assert login(code=codes[0]).json()["error"]["code"] == "invalid_code"
    templates = [e.payload["template"] for e in db.session.query(m.OutboxEvent).filter_by(kind="system_email")
                 if e.payload["to"] == user.email]
    assert "two_factor_changed" in templates

    # Lost phone and codes: support resets it.
    sign_in(admin_client)
    detail = admin_client.post(f"/v1/admin/users/{user.user_id}/reset-two-factor",
                               json={"reason": "Lost phone; confirmed by reply from their address"}).json()
    assert detail["user"]["two_factor_enabled"] is False
    assert login().status_code == 200


def test_turning_two_factor_off_needs_password_and_code(make_user) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    secret = user.post("/v1/auth/two-factor/setup").json()["secret"]
    user.post("/v1/auth/two-factor/confirm", json={"code": totp_code(secret, totp_step())})
    assert user.post("/v1/auth/two-factor/disable", json={"password": "wrong", "code": "1"}).status_code == 422
    assert user.post("/v1/auth/two-factor/recovery-codes", json={"password": PASSWORD}).status_code == 200
    off = user.post("/v1/auth/two-factor/disable", json={"password": PASSWORD,
                                                         "code": totp_code(secret, totp_step() + 1)})
    assert off.status_code == 204
    assert user.get("/v1/auth/me").json()["user"]["two_factor_enabled"] is False


def test_worker_heartbeat_pings_the_outside_monitor(transport, storage, resolver, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.worker import Worker

    pings: list[str] = []
    assert "heartbeat" not in [job.name for job in Worker(sql_uow, transport, storage, resolver).jobs]  # off by default

    monkeypatch.setattr(get_settings(), "heartbeat_url", "https://uptime.example/api/v1/heartbeat/abc")
    worker = Worker(sql_uow, transport, storage, resolver, ping=pings.append)
    job = next(job for job in worker.jobs if job.name == "heartbeat")
    worker.run_job(job)
    assert pings == ["https://uptime.example/api/v1/heartbeat/abc"]

    def unreachable(url: str) -> None:
        raise OSError("connection refused")

    failing = Worker(sql_uow, transport, storage, resolver, ping=unreachable)
    assert next(job for job in failing.jobs if job.name == "heartbeat").run(None) == "failed"  # logged, not raised

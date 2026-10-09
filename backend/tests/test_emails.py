from datetime import UTC, datetime

import pytest

from app import models as m
from app.services import emails
from app.services.sending import system_email
from tests.conftest import CSRF, last_link_token, register, sql_uow

URL = "https://www.mailvender.com/verify-email?token=abc&x=1"

ALL = {
    "verify_email": lambda: emails.verify_email(URL, 48),
    "password_reset": lambda: emails.password_reset(URL, 60),
    "password_changed": lambda: emails.password_changed("ada@example.com", datetime(2026, 10, 8, 9, 30, tzinfo=UTC),
                                                        URL),
    "already_registered": lambda: emails.already_registered(URL, URL),
    "welcome": lambda: emails.welcome(URL),
    "invitation": lambda: emails.invitation("ada@example.com", "Acme", "bob@example.com", URL, 7),
}


@pytest.mark.parametrize("name", ALL)
def test_every_system_email_has_text_and_responsive_html(name: str) -> None:
    email = ALL[name]()
    assert email.subject
    assert URL in email.text
    html = email.html
    assert html.startswith("<!DOCTYPE html>")
    assert 'href="https://www.mailvender.com/verify-email?token=abc&amp;x=1"' in html  # escaped attribute
    assert 'name="viewport"' in html and "@media only screen and (max-width: 620px)" in html
    assert "prefers-color-scheme: dark" in html
    assert len(html.encode()) < 102_000  # Gmail clips larger messages


def test_user_supplied_names_cannot_inject_markup() -> None:
    evil = '<a href="https://evil.example">Click</a><script>alert(1)</script>'
    html = emails.invitation(evil, evil, "bob@example.com", URL, 7).html
    assert "<script>" not in html
    assert 'href="https://evil.example"' not in html
    assert "&lt;script&gt;" in html


def test_system_email_is_multipart_with_text_and_html() -> None:
    email = emails.verify_email(URL, 48)
    mime = system_email({"to": "ada@example.com", "subject": email.subject, "text": email.text, "html": email.html})
    assert mime.get_content_type() == "multipart/alternative"
    parts = {part.get_content_type(): part.get_content() for part in mime.iter_parts()}
    assert URL in parts["text/plain"]
    assert "Confirm my email" in parts["text/html"]
    # Events queued before HTML templates existed still send as plain text.
    legacy = system_email({"to": "ada@example.com", "subject": "Hi", "text": "Hello"})
    assert legacy.get_content_type() == "text/plain"


def last_payload(email: str, template: str) -> dict[str, str]:
    with sql_uow() as uow:
        rows = uow.session.query(m.OutboxEvent).filter(m.OutboxEvent.kind == "system_email").order_by(
            m.OutboxEvent.created_at.desc()).all()
    for row in rows:
        if row.payload.get("to") == email and row.payload.get("template") == template:
            return row.payload
    raise AssertionError(f"No {template} email for {email}")


def test_verification_sends_welcome_and_reset_sends_security_alert(client) -> None:  # type: ignore[no-untyped-def]
    register(client, "hello@example.com")
    welcome = last_payload("hello@example.com", "welcome")
    assert welcome["html"] and "Open the studio" in welcome["html"]

    client.post("/v1/auth/password-reset/request", json={"email": "hello@example.com"}, headers=CSRF)
    token = last_link_token("hello@example.com", "password_reset")
    response = client.post("/v1/auth/password-reset/confirm", json={"token": token, "password": "a new password!!"},
                           headers=CSRF)
    assert response.status_code == 204
    alert = last_payload("hello@example.com", "password_changed")
    assert "hello@example.com" in alert["text"]
    assert "/forgot-password" in alert["text"]

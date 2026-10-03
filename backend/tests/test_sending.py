import threading
import uuid
from email import message_from_bytes, policy

from app import models as m
from app.config import get_settings
from app.services.mta import PermanentDeliveryError
from tests.conftest import send, verified_sender

BULK_HTML = '<p>Our news.</p><p><a href="{{unsubscribe_url}}">Unsubscribe</a> · {{physical_address}}</p>'
BULK_TEXT = "Our news.\n\nUnsubscribe: {{unsubscribe_url}}\n{{physical_address}}"


def statuses(session, message_id):  # type: ignore[no-untyped-def]
    return [r["status"] for r in session.get(f"/v1/messages/{message_id}").json()["recipients"]]


def test_send_is_queued_then_accepted_by_mta(make_user, resolver, worker, transport, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    response = send(user, setup["identity"]["email"], ["A@Example.org", "b@example.org"],
                    metadata={"campaign": "launch"})
    assert response.status_code == 202
    message = response.json()
    assert [r["email"] for r in message["recipients"]] == ["a@example.org", "b@example.org"]
    assert {r["status"] for r in message["recipients"]} == {"queued"}
    # Recipient records and outbox events were committed with the message.
    assert db.session.query(m.OutboxEvent).filter_by(kind="deliver_recipient").count() == 2
    recipient = db.session.query(m.MessageRecipient).first()
    assert recipient.account_id == uuid.UUID(user.account_id) and recipient.domain_id

    assert worker.process_outbox() == 2
    assert statuses(user, message["id"]) == ["accepted_by_mta", "accepted_by_mta"]
    envelope_from, rcpts, raw = transport.sent[0]
    assert envelope_from.startswith("b-") and envelope_from.endswith(f"@bounce.{setup['domain']['name']}")
    mime = message_from_bytes(raw, policy=policy.default)
    assert mime["From"] == f"News <{setup['identity']['email']}>"
    assert mime["X-Mailvender-Message-Id"] == message["id"]
    assert mime.get_body(("plain",)).get_content().strip() == "Hello there, friend."
    assert "List-Unsubscribe" not in mime  # transactional
    attempts = db.session.query(m.DeliveryAttempt).all()
    assert {a.outcome for a in attempts} == {"accepted"}


def test_bulk_mail_gets_one_click_unsubscribe_headers(make_user, resolver, worker, transport) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    user.patch("/v1/account", json={"physical_address": "1 Market St, Lagos"})
    setup = verified_sender(user, resolver)
    response = send(user, setup["identity"]["email"], ["r@example.org"], category="bulk", html=BULK_HTML,
                    text=BULK_TEXT)
    assert response.status_code == 202, response.text
    worker.process_outbox()
    raw = transport.sent[0][2]
    # Not RFC 2047-encoded: mail clients need the URL as is.
    assert b"\r\nList-Unsubscribe: <https://app.test/api/u/" in raw
    mime = message_from_bytes(raw, policy=policy.default)
    assert mime["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    url = mime["List-Unsubscribe"].strip("<>")
    assert url.startswith("https://app.test/api/u/")
    html = mime.get_body(("html",)).get_content()
    assert url in html.replace("&amp;", "&") and "1 Market St, Lagos" in html
    assert "{{" not in html and "{{" not in mime.get_body(("plain",)).get_content()


def test_bulk_requirements_are_enforced_before_queueing(make_user, resolver, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver, dmarc=None)
    response = send(user, setup["identity"]["email"], ["r@example.org"], category="bulk", html="<p>Buy!</p>")
    assert response.status_code == 422
    codes = {f["code"] for f in response.json()["error"]["fields"]}
    assert codes == {"bulk_not_eligible", "text_required", "unsubscribe_required", "physical_address_missing"}
    assert db.session.query(m.Message).count() == 0


def test_idempotency(make_user, resolver, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    first = send(user, setup["identity"]["email"], ["r@example.org"], key="order-1")
    again = send(user, setup["identity"]["email"], ["r@example.org"], key="order-1")
    assert first.status_code == again.status_code == 202
    assert first.json()["id"] == again.json()["id"]
    assert db.session.query(m.Message).count() == 1

    different = send(user, setup["identity"]["email"], ["other@example.org"], key="order-1")
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "idempotency_key_reused"
    missing = user.post("/v1/messages", json={"from": setup["identity"]["email"], "to": ["r@example.org"],
                                              "subject": "s", "html": "<p>x</p>"})
    assert missing.status_code == 422
    assert missing.json()["error"]["fields"][0]["code"] == "idempotency_key_required"


def test_validation_is_all_or_nothing_with_field_errors(make_user, resolver, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    user.post("/v1/suppressions", json={"email": "gone@example.org"})
    response = send(user, setup["identity"]["email"], ["ok@example.org", "nope", "gone@example.org",
                                                       "ok@example.org"], subject="Two\nlines")
    assert response.status_code == 422
    fields = {(f["field"], f["code"]) for f in response.json()["error"]["fields"]}
    assert fields == {("to[1]", "invalid_email"), ("to[2]", "suppressed"), ("to[3]", "duplicate"),
                      ("subject", "invalid")}
    suppressed = next(f for f in response.json()["error"]["fields"] if f["code"] == "suppressed")
    assert "unsubscribe" not in suppressed["message"].lower()  # non-sensitive
    assert db.session.query(m.Message).count() == 0
    assert db.session.query(m.OutboxEvent).filter_by(kind="deliver_recipient").count() == 0

    too_many = send(user, setup["identity"]["email"], [f"r{i}@example.org" for i in range(51)])
    assert too_many.json()["error"]["fields"][0]["code"] == "too_many_recipients"
    huge = send(user, setup["identity"]["email"], ["r@example.org"], html="x" * 1_000_001)
    assert huge.json()["error"]["fields"][0]["code"] == "too_large"


def test_temporary_failures_retry_with_backoff_then_fail(make_user, resolver, worker, transport, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    message = send(user, setup["identity"]["email"], ["r@example.org"]).json()
    transport.fail_temporarily(1)
    worker.process_outbox()
    event = db.session.query(m.OutboxEvent).filter_by(kind="deliver_recipient").one()
    assert event.status == "pending" and event.available_at > event.created_at
    assert statuses(user, message["id"]) == ["queued"]
    assert worker.process_outbox() == 0  # not due yet

    limit = get_settings().delivery_max_attempts
    transport.fail_temporarily(limit)
    for _ in range(limit):
        db.session.execute(m.OutboxEvent.__table__.update().values(available_at=event.created_at))
        db.commit()
        worker.process_outbox()
    recipient = user.get(f"/v1/messages/{message['id']}").json()["recipients"][0]
    assert recipient["status"] == "failed" and recipient["attempts"] == limit
    assert "Gave up" in recipient["last_error"]
    assert db.session.query(m.DeliveryAttempt).count() == limit


def test_permanent_mta_rejection_fails_immediately(make_user, resolver, worker, transport) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    message = send(user, setup["identity"]["email"], ["r@example.org"]).json()
    transport.failures = [PermanentDeliveryError("550 no", 550)]
    worker.process_outbox()
    assert statuses(user, message["id"]) == ["failed"]


def test_suppressed_after_queueing_is_not_delivered(make_user, resolver, worker, transport) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    message = send(user, setup["identity"]["email"], ["late@example.org"]).json()
    user.post("/v1/suppressions", json={"email": "late@example.org"})
    worker.process_outbox()
    assert statuses(user, message["id"]) == ["suppressed"]
    assert transport.sent == []


def test_sending_limits_hold_under_concurrency(make_user, resolver, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    account = db.session.get(m.Account, user.account_id)
    account.hourly_recipient_limit = 5
    db.commit()

    from fastapi.testclient import TestClient

    from app.main import app

    results: list[int] = []
    cookie = user.client.cookies.get("mv_session")

    def attempt(index: int) -> None:
        with TestClient(app, base_url="https://testserver") as c:
            response = c.post("/v1/messages", headers={**user.headers, "Idempotency-Key": f"c{index}",
                                                       "Cookie": f"mv_session={cookie}"},
                              json={"from": setup["identity"]["email"], "to": [f"r{index}@example.org"],
                                    "subject": "s", "html": "<p>hello</p>"})
            results.append(response.status_code)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [202] * 5 + [429] * 5
    assert db.session.query(m.MessageRecipient).count() == 5
    response = send(user, setup["identity"]["email"], ["x@example.org"])
    assert response.json()["error"]["code"] == "sending_limit_exceeded"
    assert response.headers["Retry-After"] == "3600"


def test_repeated_limit_violations_suspend_the_account(make_user, resolver, db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    monkeypatch.setattr(get_settings(), "limit_violations_before_suspension", 3)
    account = db.session.get(m.Account, user.account_id)
    account.hourly_recipient_limit = 0
    db.commit()
    for _ in range(3):
        assert send(user, setup["identity"]["email"], ["r@example.org"]).status_code == 429
    response = send(user, setup["identity"]["email"], ["r@example.org"])
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_suspended"
    # Suspended users can still sign in and read their data.
    assert user.get("/v1/account").json()["sending_suspended_at"]
    assert user.get("/v1/messages").status_code == 200


def test_api_key_can_send(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    key = user.post("/v1/account/api-keys", json={"name": "server"}).json()["key"]
    user.client.cookies.clear()
    response = user.client.post("/v1/messages", headers={"Authorization": f"Bearer {key}", "Idempotency-Key": "a"},
                                json={"from": setup["identity"]["email"], "to": ["r@example.org"], "subject": "Hi",
                                      "html": "<p>From the API</p>"})
    assert response.status_code == 202
    message = response.json()
    assert user.client.get(f"/v1/messages/{message['id']}",
                           headers={"Authorization": f"Bearer {key}"}).json()["id"] == message["id"]


def test_message_lifecycle_logs_carry_message_id(make_user, resolver, worker, caplog) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    message = send(user, setup["identity"]["email"], ["r@example.org"]).json()
    worker.process_outbox()
    events = {getattr(r, "event", None): getattr(r, "message_id", None) for r in caplog.records}
    assert events["message.queued"] == message["id"]
    assert events["message.accepted_by_mta"] == message["id"]

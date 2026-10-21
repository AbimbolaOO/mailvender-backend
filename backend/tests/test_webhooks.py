import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from app import models as m
from app.security import utcnow
from app.services import webhooks
from app.services.webhooks import MAX_ATTEMPTS, Response
from app.worker import Worker
from tests.conftest import sql_uow
from tests.test_feedback import delivered, dsn

URL = "https://hooks.customer.example/mailvender"


class Receiver:
    """A customer's endpoint: records requests and answers with `status`."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []
        self.status = 200

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> Response:
        self.requests.append((url, body, headers))
        return Response(self.status, "ok" if self.status < 300 else "nope")

    def events(self) -> list[dict]:  # type: ignore[type-arg]
        return [json.loads(body) for _, body, _ in self.requests]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):  # type: ignore[no-untyped-def]
    """Hosts resolve to a public address unless a test says otherwise (no network in tests)."""
    monkeypatch.setattr(webhooks, "resolve_host", lambda host: ["93.184.216.34"])


@pytest.fixture
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture
def hook_worker(transport, storage, resolver, receiver) -> Worker:  # type: ignore[no-untyped-def]
    return Worker(sql_uow, transport, storage, resolver, webhook_post=receiver)


def add_endpoint(user, events=("message.sent", "message.bounced", "recipient.unsubscribed")):  # type: ignore[no-untyped-def]
    response = user.post("/v1/webhooks", json={"url": URL, "events": list(events), "description": "CRM"})
    assert response.status_code == 201, response.text
    return response.json()


def test_endpoints_are_validated(make_user, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    created = add_endpoint(user)
    assert created["secret"].startswith("whsec_") and created["endpoint"]["events"] == [
        "message.bounced", "message.sent", "recipient.unsubscribed"]
    assert "secret" not in user.get("/v1/webhooks").json()[0]
    assert {e["type"] for e in user.get("/v1/webhooks/events").json()} >= {"message.sent", "message.complained"}

    def problem(url: str, events: list[str] | None = None) -> str:  # type: ignore[no-untyped-def]
        body = user.post("/v1/webhooks", json={"url": url, "events": events or ["message.sent"]}).json()
        return body["error"]["fields"][0]["code"]

    assert problem("http://hooks.customer.example/x") == "invalid_url"  # https only
    assert problem(URL, ["message.opened"]) == "invalid"
    for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"):
        monkeypatch.setattr(webhooks, "resolve_host", lambda host, a=address: [a])
        assert problem("https://sneaky.example/x") == "invalid_url"  # no internal networks or cloud metadata


def test_events_are_signed_and_delivered(make_user, resolver, hook_worker, transport, receiver, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    secret = add_endpoint(user)["secret"]
    message, verp, rid, _ = delivered(user, resolver, hook_worker, transport)
    hook_worker.process_outbox()  # the webhook queued by the delivery

    url, body, headers = receiver.requests[-1]
    event = json.loads(body)
    assert url == URL and event["type"] == "message.sent" == headers["Mailvender-Event"]
    assert event["data"]["message_id"] == message["id"] and event["data"]["email"] == "r@example.org"
    assert headers["Mailvender-Event-Id"] == event["id"]
    timestamp, mac = (part.split("=", 1)[1] for part in headers["Mailvender-Signature"].split(","))
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(mac, expected)

    dsn(user.client, verp, rid, "r@example.org")
    hook_worker.process_outbox()
    bounced = receiver.events()[-1]
    assert bounced["type"] == "message.bounced" and bounced["data"]["bounce"]["classification"] == "hard"

    endpoint_id = user.get("/v1/webhooks").json()[0]["id"]
    history = user.get(f"/v1/webhooks/{endpoint_id}/deliveries").json()["items"]
    assert [d["event_type"] for d in history] == ["message.bounced", "message.sent"]
    assert all(d["status"] == "delivered" and d["response_status"] == 200 for d in history)


def test_only_subscribed_events_are_sent(make_user, resolver, hook_worker, transport, receiver) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    add_endpoint(user, events=["message.complained"])
    delivered(user, resolver, hook_worker, transport)
    hook_worker.process_outbox()
    assert receiver.requests == []


def test_failures_are_retried_then_the_endpoint_is_turned_off(make_user, resolver, hook_worker, transport, receiver,
                                                              db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    endpoint_id = add_endpoint(user)["endpoint"]["id"]
    receiver.status = 500
    delivered(user, resolver, hook_worker, transport)
    hook_worker.process_outbox()
    delivery = db.session.query(m.WebhookDelivery).one()
    assert (delivery.status, delivery.attempts, delivery.response_status) == ("pending", 1, 500)
    event = db.session.query(m.OutboxEvent).filter_by(kind="webhook").one()
    assert event.status == "pending" and event.available_at > utcnow() + timedelta(seconds=20)

    # Works on the next try.
    receiver.status = 200
    db.session.query(m.OutboxEvent).filter_by(kind="webhook").update({"available_at": utcnow()})
    db.commit()
    hook_worker.process_outbox()
    db.session.expire_all()
    assert db.session.query(m.WebhookDelivery).one().status == "delivered"

    # Failing for days: the last attempt gives up and turns the endpoint off.
    receiver.status = 503
    with sql_uow() as uow:
        uow.session.query(m.WebhookEndpoint).update({"last_success_at": utcnow() - timedelta(days=4)})
        uow.commit()
    send = user.post("/v1/webhooks/" + endpoint_id + "/test")  # tests are tried once…
    assert send.status_code == 202
    hook_worker.process_outbox()
    delivered(user, resolver, hook_worker, transport, to="other@example.org")
    db.session.query(m.WebhookDelivery).filter_by(status="pending").update({"attempts": MAX_ATTEMPTS - 1})
    db.commit()
    hook_worker.process_outbox()
    endpoint = user.get(f"/v1/webhooks/{endpoint_id}").json()
    assert endpoint["enabled"] is False and "every event failed" in endpoint["disabled_reason"]
    emails = [e.payload["template"] for e in db.session.query(m.OutboxEvent).filter_by(kind="system_email")
              if e.payload["to"] == user.email]
    assert "webhook_disabled" in emails

    # Turn it back on and resend the failed one.
    assert user.patch(f"/v1/webhooks/{endpoint_id}", json={"enabled": True}).json()["consecutive_failures"] == 0
    failed = next(d for d in user.get(f"/v1/webhooks/{endpoint_id}/deliveries").json()["items"]
                  if d["status"] == "failed" and d["event_type"] == "message.sent")
    receiver.status = 200
    assert user.post(f"/v1/webhooks/{endpoint_id}/deliveries/{failed['id']}/retry").status_code == 202
    hook_worker.process_outbox()
    assert receiver.requests[-1][2]["Mailvender-Event-Id"] == failed["event_id"]  # same id: receivers dedupe


def test_requests_to_internal_addresses_are_never_sent(make_user, resolver, hook_worker, transport, receiver, db,
                                                       monkeypatch) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    add_endpoint(user)
    monkeypatch.setattr(webhooks, "resolve_host", lambda host: ["10.0.0.7"])  # DNS changed after it was saved
    delivered(user, resolver, hook_worker, transport)
    hook_worker.process_outbox()
    assert receiver.requests == []
    assert "public internet" in db.session.query(m.WebhookDelivery).one().error


def test_owner_only_and_rotation(make_user) -> None:  # type: ignore[no-untyped-def]
    from tests.test_accounts import invite_and_accept, team

    owner, member = make_user(), make_user()
    team(owner)
    invite_and_accept(owner, member)
    member.account_id = owner.account_id
    assert member.get("/v1/webhooks").status_code == 403
    created = add_endpoint(owner)
    rotated = owner.post(f"/v1/webhooks/{created['endpoint']['id']}/rotate-secret").json()
    assert rotated["secret"] != created["secret"]
    assert owner.delete(f"/v1/webhooks/{created['endpoint']['id']}").status_code == 204
    assert owner.get("/v1/webhooks").json() == []

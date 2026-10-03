import hashlib
import hmac
from email import message_from_bytes, policy

from app import models as m
from tests.conftest import send, verified_sender

INTERNAL = {"X-Internal-Token": "internal-test-token"}

DSN = """From: MAILER-DAEMON@mx.example.org
To: {verp}
Subject: Undelivered Mail Returned to Sender
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="b"

--b
Content-Type: text/plain

Delivery failed.

--b
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.example.org

Final-Recipient: rfc822; {rcpt}
Action: {action}
Status: {status}
Diagnostic-Code: smtp; {status} mailbox problem

--b
Content-Type: text/rfc822-headers

X-Mailvender-Recipient-Id: {rid}
Subject: Hello

--b--
"""

ARF = """From: fbl@isp.example
To: fbl@mailvender.test
Subject: complaint
MIME-Version: 1.0
Content-Type: multipart/report; report-type=feedback-report; boundary="f"

--f
Content-Type: text/plain

This is an abuse report.

--f
Content-Type: message/feedback-report

Feedback-Type: abuse
Version: 1

--f
Content-Type: message/rfc822

From: news@example.com
X-Mailvender-Recipient-Id: {rid}
Subject: Hello

body
--f--
"""


def delivered(user, resolver, worker, transport, to="r@example.org"):  # type: ignore[no-untyped-def]
    setup = verified_sender(user, resolver)
    message = send(user, setup["identity"]["email"], [to]).json()
    worker.process_outbox()
    mime = message_from_bytes(transport.sent[-1][2], policy=policy.default)
    return message, transport.sent[-1][0], mime["X-Mailvender-Recipient-Id"], setup


def dsn(client, verp, rid, rcpt, status="5.1.1", action="failed"):  # type: ignore[no-untyped-def]
    body = DSN.format(verp=verp, rid=rid, rcpt=rcpt, status=status, action=action)
    return client.post(f"/internal/dsn?recipient={verp}", content=body.encode(), headers=INTERNAL)


def test_one_click_unsubscribe_is_idempotent_and_blocks_future_sends(make_user, resolver, worker, transport, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    user.patch("/v1/account", json={"physical_address": "1 Main St"})
    setup = verified_sender(user, resolver)
    html = '<a href="{{unsubscribe_url}}">Unsubscribe</a> {{physical_address}}'
    send(user, setup["identity"]["email"], ["fan@example.org"], category="bulk", html=html,
         text="Unsubscribe here: {{unsubscribe_url}}")
    worker.process_outbox()
    url = message_from_bytes(transport.sent[0][2], policy=policy.default)["List-Unsubscribe"].strip("<>")
    path = url.removeprefix("https://app.test/api")

    client = user.client
    client.cookies.clear()
    page = client.get(path)
    assert page.status_code == 200 and "<form" in page.text
    assert db.session.query(m.Suppression).count() == 0  # GET alone never unsubscribes
    for _ in range(2):
        response = client.post(path, content=b"List-Unsubscribe=One-Click",
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert response.status_code == 200 and "unsubscribed" in response.text
    suppression = db.session.query(m.Suppression).one()
    assert (suppression.email, suppression.reason, suppression.source) == ("fan@example.org", "unsubscribe",
                                                                           "one_click")
    assert client.post(path[:-4] + "AAAA").status_code == 400  # tampered token

    login_again = user.client
    from tests.conftest import login

    session = login(login_again, user.email)
    blocked = send(session, setup["identity"]["email"], ["fan@example.org"])
    assert blocked.json()["error"]["fields"][0] == {
        "field": "to[0]", "code": "suppressed", "message": "This recipient can't receive email from this account."}
    listed = session.get("/v1/suppressions").json()["items"]
    assert listed[0]["reason"] == "unsubscribe"
    audit = [e["action"] for e in session.get("/v1/account/audit-events").json()["items"]]
    assert "suppression.created" in audit


def test_view_in_browser_link(make_user, resolver, worker, transport) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    send(user, setup["identity"]["email"], ["r@example.org"], html='<p>Hi <a href="{{view_in_browser_url}}">web</a></p>')
    worker.process_outbox()
    html = message_from_bytes(transport.sent[0][2], policy=policy.default).get_body(("html",)).get_content()
    path = html.split('href="', 1)[1].split('"', 1)[0].replace("&amp;", "&").removeprefix("https://app.test/api")
    response = user.client.get(path)
    assert response.status_code == 200 and "<p>Hi" in response.text
    assert "script-src 'none'" in response.headers["content-security-policy"]


def test_hard_bounce_suppresses_immediately(make_user, resolver, worker, transport, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    message, verp, rid, _ = delivered(user, resolver, worker, transport)
    response = dsn(user.client, verp, rid, "r@example.org")
    assert response.json() == {"correlated": True, "classification": "hard", "suppressed": True}
    assert user.get(f"/v1/messages/{message['id']}").json()["recipients"][0]["status"] == "bounced"
    assert db.session.query(m.Suppression).one().reason == "hard_bounce"
    actions = [e["action"] for e in user.get("/v1/account/audit-events").json()["items"]]
    assert "delivery.bounce_classified" in actions and "suppression.created" in actions


def test_soft_bounces_suppress_only_past_threshold(make_user, resolver, worker, transport, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    _, verp, rid, _ = delivered(user, resolver, worker, transport)
    for expected in (False, False, True):
        result = dsn(user.client, verp, rid, "r@example.org", status="4.2.2", action="failed").json()
        assert result["classification"] == "soft"
        assert result["suppressed"] is expected
    assert db.session.query(m.Suppression).one().reason == "soft_bounce_threshold"
    events = db.session.query(m.DeliveryEvent).all()
    assert len(events) == 3 and {e.classification for e in events} == {"soft"}


def test_dsn_requires_internal_token_and_handles_unknown(make_user, resolver, worker, transport) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    _, verp, rid, _ = delivered(user, resolver, worker, transport)
    body = DSN.format(verp=verp, rid=rid, rcpt="r@example.org", status="5.1.1", action="failed").encode()
    assert user.client.post("/internal/dsn", content=body).status_code == 401
    assert user.client.post("/internal/dsn", content=body, headers={"X-Internal-Token": "wrong"}).status_code == 401
    # Correlated through the attached headers when the envelope isn't given.
    assert user.client.post("/internal/dsn", content=body, headers=INTERNAL).json()["correlated"] is True
    unknown = DSN.format(verp="x@y", rid="nope", rcpt="r@example.org", status="5.1.1", action="failed").encode()
    assert user.client.post("/internal/dsn", content=unknown, headers=INTERNAL).json()["correlated"] is False


def test_complaints_are_authenticated_and_suppress(make_user, resolver, worker, transport, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    message, _, rid, _ = delivered(user, resolver, worker, transport)
    body = ARF.format(rid=rid).encode()
    assert user.client.post("/internal/feedback-loop/examplefbl", content=body,
                            headers={"X-Mailvender-Signature": "sha256=bad"}).status_code == 401
    assert user.client.post("/internal/feedback-loop/unknown", content=body).status_code == 401
    signature = "sha256=" + hmac.new(b"fbl-secret", body, hashlib.sha256).hexdigest()
    response = user.client.post("/internal/feedback-loop/examplefbl", content=body,
                                headers={"X-Mailvender-Signature": signature})
    assert response.json() == {"correlated": True, "classification": "complaint", "suppressed": True}
    assert user.get(f"/v1/messages/{message['id']}").json()["recipients"][0]["status"] == "complained"
    suppression = db.session.query(m.Suppression).one()
    assert (suppression.reason, suppression.source) == ("complaint", "fbl:examplefbl")
    actions = [e["action"] for e in user.get("/v1/account/audit-events").json()["items"]]
    assert "delivery.complaint_received" in actions


def test_protected_suppressions_cannot_be_removed(make_user, resolver, worker, transport) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    _, verp, rid, _ = delivered(user, resolver, worker, transport)
    dsn(user.client, verp, rid, "r@example.org")
    assert user.delete("/v1/suppressions/r@example.org").json()["error"]["code"] == "suppression_protected"
    user.post("/v1/suppressions", json={"email": "manual@example.org"})
    assert user.delete("/v1/suppressions/manual@example.org").status_code == 204

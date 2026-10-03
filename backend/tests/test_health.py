from app import models as m
from app.security import utcnow


def add_volume(db, account_id, accepted: int, hard_bounces: int = 0, complaints: int = 0) -> None:  # type: ignore[no-untyped-def]
    """Inserts recipient-level history directly (fast) for rate calculations."""
    import uuid

    domain = m.Domain(account_id=account_id, name=f"h{uuid.uuid4().hex[:6]}.example", status="verified",
                      return_path_host="bounce.x", record_status={})
    db.session.add(domain)
    db.session.flush()
    identity = m.SenderIdentity(account_id=account_id, domain_id=domain.id, email=f"a@{domain.name}",
                                status="verified")
    db.session.add(identity)
    db.session.flush()
    message = m.Message(account_id=account_id, domain_id=domain.id, sender_identity_id=identity.id,
                        from_email=identity.email, subject="s", html="h", text_body="t", category="bulk",
                        idempotency_key=uuid.uuid4().hex, request_hash="x")
    db.session.add(message)
    db.session.flush()
    now = utcnow()
    for index in range(accepted):
        recipient = m.MessageRecipient(message_id=message.id, account_id=account_id, domain_id=domain.id,
                                       email=f"r{index}@example.org", status="accepted_by_mta", accepted_at=now)
        db.session.add(recipient)
        db.session.flush()
        if index < hard_bounces:
            db.session.add(m.DeliveryEvent(account_id=account_id, recipient_id=recipient.id, email=recipient.email,
                                           type="bounce", classification="hard", source="dsn"))
        elif index < hard_bounces + complaints:
            db.session.add(m.DeliveryEvent(account_id=account_id, recipient_id=recipient.id, email=recipient.email,
                                           type="complaint", source="fbl:x"))
    db.commit()


def make_operator(db, session) -> None:  # type: ignore[no-untyped-def]
    user = db.users.get(session.user_id)
    user.is_operator = True
    db.commit()


def test_bounce_rate_suspends_only_above_minimum_volume(make_user, worker, db) -> None:  # type: ignore[no-untyped-def]
    small, big = make_user(), make_user()
    add_volume(db, small.account_id, accepted=10, hard_bounces=5)
    add_volume(db, big.account_id, accepted=100, hard_bounces=6)
    worker.run_job(next(j for j in worker.jobs if j.name == "account_health"))
    assert small.get("/v1/account").json()["sending_suspended_at"] is None
    account = big.get("/v1/account").json()
    assert account["sending_suspended_at"] and "Bounce rate 6.00%" in account["suspension_reason"]


def test_complaint_rate_suspends(make_user, worker, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    add_volume(db, user.account_id, accepted=200, complaints=1)
    worker.run_job(next(j for j in worker.jobs if j.name == "account_health"))
    assert "Complaint rate" in user.get("/v1/account").json()["suspension_reason"]
    assert db.session.query(m.JobRun).filter_by(name="account_health").one().last_error is None


def test_operator_api_is_operator_only_and_audited(make_user, db) -> None:  # type: ignore[no-untyped-def]
    operator, customer = make_user(), make_user()
    assert customer.get("/v1/operator/accounts").status_code == 403
    make_operator(db, operator)
    add_volume(db, customer.account_id, accepted=20, hard_bounces=1)

    listing = operator.get("/v1/operator/accounts?limit=100").json()
    row = next(i for i in listing["items"] if i["account"]["id"] == customer.account_id)
    assert row["metrics"]["accepted"] == 20 and row["metrics"]["bounce_rate"] == 0.05
    assert row["status"] == "active" and row["latest_decision"] is None

    path = f"/v1/operator/accounts/{customer.account_id}"
    assert operator.post(f"{path}/suspend", json={"reason": "Spam trap hits"}).json()["status"] == "suspended"
    limits = operator.put(f"{path}/limits", json={"hourly_recipient_limit": 10, "daily_recipient_limit": 50,
                                                  "reason": "New sender"}).json()
    assert limits["account"]["hourly_recipient_limit"] == 10
    reviewed = operator.post(f"{path}/reviews", json={"reason": "Checked list source"}).json()
    assert reviewed["latest_decision"]["action"] == "account.reviewed"
    reinstated = operator.post(f"{path}/reinstate", json={"reason": "List cleaned"}).json()
    assert reinstated["status"] == "active"
    assert reinstated["latest_decision"]["actor_type"] == "operator"
    assert reinstated["latest_decision"]["reason"] == "List cleaned"

    actions = [e.action for e in db.session.query(m.AuditEvent).filter_by(account_id=customer.account_id)]
    for action in ("account.suspended", "account.limits_changed", "account.reviewed", "account.reinstated"):
        assert action in actions

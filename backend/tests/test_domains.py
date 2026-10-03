from datetime import timedelta

from app import models as m
from app.security import decrypt_secret, utcnow
from tests.conftest import publish_dns, send, verified_sender


def records(domain: dict) -> dict:
    return {r["key"]: r for r in domain["records"]}


def test_register_generates_dkim_key_and_records(make_user, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    response = user.post("/v1/domains", json={"name": "Example.COM."})
    assert response.status_code == 201
    domain = response.json()
    assert domain["name"] == "example.com" and domain["status"] == "pending"
    recs = records(domain)
    assert set(recs) == {"dkim", "return_path_mx", "spf", "dmarc"}
    assert recs["dkim"]["name"] == f"{domain['dkim_selector']}._domainkey.example.com"
    assert recs["dkim"]["value"].startswith("v=DKIM1; k=rsa; p=")
    assert recs["spf"]["name"] == recs["return_path_mx"]["name"] == "bounce.example.com"
    assert "include:" in recs["spf"]["value"]
    assert recs["dmarc"]["required"] is False

    key = db.session.query(m.DkimKey).one()
    assert "PRIVATE KEY" not in key.private_key_encrypted  # encrypted at rest
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    assert load_pem_private_key(decrypt_secret(key.private_key_encrypted).encode(), None).key_size == 2048
    assert "PRIVATE" not in str(domain)


def test_invalid_and_duplicate_domains(make_user) -> None:  # type: ignore[no-untyped-def]
    alice, bob = make_user(), make_user()
    assert alice.post("/v1/domains", json={"name": "not a domain"}).status_code == 422
    assert alice.post("/v1/domains", json={"name": "shared.example"}).status_code == 201
    mine = alice.post("/v1/domains", json={"name": "shared.example"})
    assert mine.status_code == 409 and mine.json()["error"]["code"] == "domain_exists"
    theirs = bob.post("/v1/domains", json={"name": "shared.example"})
    assert theirs.status_code == 409
    assert theirs.json()["error"]["code"] == "domain_unavailable"
    assert alice.account_id not in theirs.text and alice.email not in theirs.text


def test_verification_reports_each_record(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    domain = user.post("/v1/domains", json={"name": "verify.example"}).json()
    result = user.post(f"/v1/domains/{domain['id']}/verify").json()
    assert result["status"] == "pending"
    assert {r["status"] for r in result["records"]} == {"missing"}

    recs = records(domain)
    resolver.txt_records[recs["dkim"]["name"]] = ["v=DKIM1; k=rsa; p=WRONGKEY"]
    resolver.txt_records["bounce.verify.example"] = ["v=spf1 include:other.example -all"]
    resolver.mx_records["bounce.verify.example"] = ["mx.other.example"]
    result = user.post(f"/v1/domains/{domain['id']}/verify").json()
    assert {k: v["status"] for k, v in records(result).items()} == {
        "dkim": "mismatched", "spf": "mismatched", "return_path_mx": "mismatched", "dmarc": "missing"}

    resolver.txt_records.clear()
    resolver.mx_records.clear()
    publish_dns(resolver, domain, dmarc=None)
    result = user.post(f"/v1/domains/{domain['id']}/verify").json()
    assert result["status"] == "verified"
    assert result["bulk_eligible"] is False  # no DMARC yet
    assert records(result)["dmarc"]["status"] == "missing"


def test_dmarc_alignment_controls_bulk_eligibility(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    strict = verified_sender(user, resolver, "strict.example", dmarc="v=DMARC1; p=reject; aspf=s")
    assert strict["domain"]["bulk_eligible"] is False
    relaxed = verified_sender(user, resolver, "relaxed.example", dmarc="v=DMARC1; p=quarantine")
    assert relaxed["domain"]["bulk_eligible"] is True


def test_dns_failure_does_not_verify(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    domain = user.post("/v1/domains", json={"name": "flaky.example"}).json()
    resolver.failing = True
    assert user.post(f"/v1/domains/{domain['id']}/verify").json()["status"] == "pending"


def test_recheck_job_reverts_domain_and_blocks_sends(make_user, resolver, worker, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver, "recheck.example")
    assert send(user, setup["identity"]["email"], ["a@example.org"]).status_code == 202

    resolver.txt_records.clear()  # DKIM and SPF records vanish
    domain = db.session.get(m.Domain, setup["domain"]["id"])
    domain.last_checked_at = utcnow() - timedelta(hours=2)
    db.commit()
    job = next(j for j in worker.jobs if j.name == "dns_recheck")
    worker.run_job(job)

    assert user.get(f"/v1/domains/{setup['domain']['id']}").json()["status"] == "pending"
    response = send(user, setup["identity"]["email"], ["b@example.org"])
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["code"] == "sender_domain_not_verified"
    actions = [e["action"] for e in user.get("/v1/account/audit-events").json()["items"]]
    assert "domain.verification_lost" in actions


def test_disabled_domain_rejects_sends(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver)
    assert user.post(f"/v1/domains/{setup['domain']['id']}/disable").json()["status"] == "disabled"
    response = send(user, setup["identity"]["email"], ["a@example.org"])
    assert response.json()["error"]["fields"][0]["code"] == "sender_domain_not_verified"
    assert user.post(f"/v1/domains/{setup['domain']['id']}/enable").json()["status"] == "pending"


def test_dkim_rotation(make_user, resolver, db) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    setup = verified_sender(user, resolver, "rotate.example")
    old_selector = setup["domain"]["dkim_selector"]
    rotated = user.post(f"/v1/domains/{setup['domain']['id']}/dkim/rotate").json()
    nxt = records(rotated)["dkim_next"]
    assert rotated["dkim_selector"] == old_selector
    assert user.post(f"/v1/domains/{setup['domain']['id']}/dkim/rotate").status_code == 409

    resolver.txt_records[nxt["name"]] = [nxt["value"]]
    result = user.post(f"/v1/domains/{setup['domain']['id']}/verify").json()
    assert result["dkim_selector"] != old_selector
    assert result["status"] == "verified"
    statuses = sorted(k.status for k in db.session.query(m.DkimKey).all())
    assert statuses == ["active", "retired"]


def test_sender_identities(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    user = make_user()
    domain = user.post("/v1/domains", json={"name": "ids.example"}).json()
    wrong = user.post("/v1/sender-identities", json={"domain_id": domain["id"], "email": "a@other.example"})
    assert wrong.status_code == 422 and wrong.json()["error"]["fields"][0]["code"] == "domain_mismatch"
    identity = user.post("/v1/sender-identities", json={"domain_id": domain["id"], "email": "hi@ids.example"}).json()
    assert identity["status"] == "pending"
    response = user.post(f"/v1/sender-identities/{identity['id']}/verify")
    assert response.status_code == 409 and response.json()["error"]["code"] == "domain_not_verified"

    publish_dns(resolver, domain)
    user.post(f"/v1/domains/{domain['id']}/verify")
    assert user.post(f"/v1/sender-identities/{identity['id']}/verify").json()["status"] == "verified"
    assert user.post(f"/v1/sender-identities/{identity['id']}/disable").json()["status"] == "disabled"
    response = send(user, "hi@ids.example", ["a@example.org"])
    assert response.json()["error"]["fields"][0]["code"] == "sender_not_approved"
    # An unapproved address on a verified domain is rejected too.
    assert send(user, "other@ids.example", ["a@example.org"]).json()["error"]["fields"][0]["code"] == \
        "sender_not_approved"
    history = user.get(f"/v1/sender-identities/{identity['id']}/history").json()["items"]
    assert [e["action"] for e in history] == ["sender_identity.disabled", "sender_identity.verified",
                                              "sender_identity.created"]

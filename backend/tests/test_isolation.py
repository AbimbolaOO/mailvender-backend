"""Cross-account reads, writes, sends, API-key use and domain use must all fail."""

import uuid

from tests.conftest import CSRF, png_bytes, send, verified_sender


def test_cross_account_reads_and_writes_fail(make_user, resolver, storage) -> None:  # type: ignore[no-untyped-def]
    alice, bob = make_user(), make_user()
    setup = verified_sender(alice, resolver)
    message = send(alice, setup["identity"]["email"], ["r@example.org"]).json()
    upload = alice.post("/v1/assets/uploads", json={"filename": "a.png", "content_type": "image/png",
                                                     "size": 100}).json()
    storage.objects[storage.presigned[-1]["key"]] = (png_bytes(), "image/png")
    asset = alice.post(f"/v1/assets/{upload['asset']['id']}/finalize").json()
    record_id = str(uuid.uuid4())
    assert alice.put(f"/v1/sync/pages/{record_id}", json={"expected_revision": 0, "data": {"x": 1}}).status_code == 200

    # Bob can't select Alice's account …
    as_alice = {"X-Account-Id": alice.account_id}
    assert bob.get("/v1/domains", headers=as_alice).status_code == 403
    assert bob.put(f"/v1/sync/pages/{record_id}", json={"expected_revision": 1, "data": {}},
                   headers=as_alice).status_code == 403
    # … and from his own account, Alice's resources don't exist.
    for path in (
        f"/v1/domains/{setup['domain']['id']}",
        f"/v1/messages/{message['id']}",
        f"/v1/sync/pages/{record_id}",
    ):
        assert bob.get(path).status_code == 404, path
    assert bob.post(f"/v1/domains/{setup['domain']['id']}/verify").status_code == 404
    assert bob.post(f"/v1/sender-identities/{setup['identity']['id']}/disable").status_code == 404
    assert bob.delete(f"/v1/assets/{asset['id']}").status_code == 404
    assert bob.post(f"/v1/assets/{asset['id']}/finalize").status_code == 404
    assert bob.put(f"/v1/sync/pages/{record_id}", json={"expected_revision": 1, "data": {}}).status_code == 409
    assert bob.get("/v1/assets").json()["items"] == []
    assert bob.get("/v1/sync/changes").json()["records"] == []
    assert alice.get(f"/v1/sync/pages/{record_id}").json()["data"] == {"x": 1}


def test_cannot_send_from_another_accounts_domain(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    alice, bob = make_user(), make_user()
    setup = verified_sender(alice, resolver)
    response = send(bob, setup["identity"]["email"], ["r@example.org"])
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0] == {
        "field": "from", "code": "sender_not_approved", "message": "Send from an approved sender of this account."}
    # Bob can't add the domain either – and isn't told whose it is.
    assert bob.post("/v1/domains", json={"name": setup["domain"]["name"]}).json()["error"]["code"] == \
        "domain_unavailable"
    # Nor an identity under Alice's domain id.
    response = bob.post("/v1/sender-identities", json={"domain_id": setup["domain"]["id"],
                                                       "email": setup["identity"]["email"]})
    assert response.status_code == 404


def test_api_keys_are_bound_to_their_account(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    alice, bob = make_user(), make_user()
    setup = verified_sender(alice, resolver)
    alice_key = alice.post("/v1/account/api-keys", json={"name": "a"}).json()["key"]
    bob_setup = verified_sender(bob, resolver)
    message = send(bob, bob_setup["identity"]["email"], ["r@example.org"]).json()

    api = {"Authorization": f"Bearer {alice_key}", **CSRF}
    alice.client.cookies.clear()
    client = alice.client
    # Naming another account is refused; the key only ever sees its own.
    response = client.get("/v1/messages", headers={**api, "X-Account-Id": bob.account_id})
    assert response.status_code == 403 and response.json()["error"]["code"] == "account_mismatch"
    assert client.get(f"/v1/messages/{message['id']}", headers=api).status_code == 404
    response = client.post("/v1/messages", headers={**api, "Idempotency-Key": "k1"}, json={
        "from": bob_setup["identity"]["email"], "to": ["x@example.org"], "subject": "s", "html": "<p>hi</p>"})
    assert response.status_code == 422
    response = client.post("/v1/messages", headers={**api, "Idempotency-Key": "k2"}, json={
        "from": setup["identity"]["email"], "to": ["x@example.org"], "subject": "s", "html": "<p>hi</p>"})
    assert response.status_code == 202
    assert client.get("/v1/suppressions", headers=api).status_code == 200
    # Keys can't use session-only endpoints.
    assert client.get("/v1/sync/changes", headers=api).status_code == 403


def test_body_cannot_choose_the_account(make_user, resolver) -> None:  # type: ignore[no-untyped-def]
    alice, bob = make_user(), make_user()
    verified_sender(alice, resolver)
    # An `account_id` in the body is ignored – the domain lands in Bob's own account.
    response = bob.post("/v1/domains", json={"name": "body.example", "account_id": alice.account_id})
    assert response.status_code == 201
    assert alice.get(f"/v1/domains/{response.json()['id']}").status_code == 404
    assert bob.get(f"/v1/domains/{response.json()['id']}").status_code == 200

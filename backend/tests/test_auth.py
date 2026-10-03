from datetime import timedelta

from app import models as m
from app.security import utcnow
from tests.conftest import CSRF, PASSWORD, last_link_token, login, register


def signup(client, email, password=PASSWORD):  # type: ignore[no-untyped-def]
    return client.post("/v1/auth/signup", json={"email": email, "password": password}, headers=CSRF)


def test_signup_normalizes_email_and_stores_argon2id_hash(client, db) -> None:  # type: ignore[no-untyped-def]
    assert signup(client, "  Ada@Example.COM ").status_code == 202
    user = db.users.get_by_email("ada@example.com")
    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash
    assert user.email_verified_at is None


def test_signup_rejects_bad_input_with_field_errors(client) -> None:  # type: ignore[no-untyped-def]
    response = signup(client, "not-an-email")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_failed"
    assert error["fields"][0]["field"] == "email"
    assert error["request_id"]
    assert signup(client, "ok@example.com", "short").json()["error"]["fields"][0]["code"] == "password_length"


def test_signup_does_not_reveal_registered_addresses(client) -> None:  # type: ignore[no-untyped-def]
    register(client, "taken@example.com")
    first = signup(client, "taken@example.com")
    second = signup(client, "new@example.com")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()


def test_unverified_users_cannot_log_in(client) -> None:  # type: ignore[no-untyped-def]
    signup(client, "pending@example.com")
    response = client.post("/v1/auth/login", json={"email": "pending@example.com", "password": PASSWORD},
                           headers=CSRF)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "email_not_verified"
    assert "mv_session" not in response.cookies


def test_verification_creates_personal_account_and_token_is_single_use(client, db) -> None:  # type: ignore[no-untyped-def]
    signup(client, "ver@example.com")
    token = last_link_token("ver@example.com", "verify_email")
    with_hash = db.session.query(m.EmailToken).one()
    assert token not in with_hash.token_hash  # only the hash is stored

    response = client.post("/v1/auth/verify-email", json={"token": token}, headers=CSRF)
    assert response.status_code == 200
    user_id = response.json()["id"]
    accounts = db.accounts.list_for_user(user_id)
    assert [(a.kind, role) for a, role in accounts] == [("personal", "owner")]
    assert response.json()["default_account_id"] == str(accounts[0][0].id)

    again = client.post("/v1/auth/verify-email", json={"token": token}, headers=CSRF)
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "invalid_token"
    assert len(db.accounts.list_for_user(user_id)) == 1


def test_expired_verification_token_is_rejected(client, db) -> None:  # type: ignore[no-untyped-def]
    signup(client, "late@example.com")
    token = last_link_token("late@example.com", "verify_email")
    record = db.session.query(m.EmailToken).one()
    record.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert client.post("/v1/auth/verify-email", json={"token": token}, headers=CSRF).status_code == 400


def test_login_sets_secure_httponly_cookie_and_me_works(client) -> None:  # type: ignore[no-untyped-def]
    register(client, "me@example.com")
    response = client.post("/v1/auth/login", json={"email": "ME@example.com", "password": PASSWORD}, headers=CSRF)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "mv_session=" in cookie and "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "token" not in response.json()  # the credential lives only in the cookie
    me = client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "me@example.com"
    assert me.json()["accounts"][0]["role"] == "owner"


def test_wrong_password_is_rejected(client) -> None:  # type: ignore[no-untyped-def]
    register(client, "pw@example.com")
    response = client.post("/v1/auth/login", json={"email": "pw@example.com", "password": "wrong password!"},
                           headers=CSRF)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_logout_revokes_the_session_immediately(client) -> None:  # type: ignore[no-untyped-def]
    register(client, "out@example.com")
    login(client, "out@example.com")
    cookie = client.cookies.get("mv_session")
    assert client.post("/v1/auth/logout", headers=CSRF).status_code == 204
    client.cookies.clear()
    response = client.get("/v1/auth/me", headers={"Cookie": f"mv_session={cookie}"})  # replay the old cookie
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_invalid"


def test_expired_session_is_rejected(client, db) -> None:  # type: ignore[no-untyped-def]
    register(client, "exp@example.com")
    login(client, "exp@example.com")
    session = db.session.query(m.Session).one()
    session.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert client.get("/v1/auth/me").status_code == 401


def test_unauthenticated_and_csrf(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/v1/auth/me").json()["error"]["code"] == "unauthenticated"
    register(client, "csrf@example.com")
    session = login(client, "csrf@example.com")
    response = client.post("/v1/accounts", json={"name": "Team"}, headers={"X-Account-Id": session.account_id})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"
    assert client.post("/v1/auth/login", json={"email": "csrf@example.com", "password": PASSWORD}).status_code == 403


def test_password_reset_flow(client) -> None:  # type: ignore[no-untyped-def]
    register(client, "reset@example.com")
    other = login(client, "reset@example.com")
    known = client.post("/v1/auth/password-reset/request", json={"email": "reset@example.com"}, headers=CSRF)
    unknown = client.post("/v1/auth/password-reset/request", json={"email": "nobody@example.com"}, headers=CSRF)
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()

    token = last_link_token("reset@example.com", "password_reset")
    response = client.post("/v1/auth/password-reset/confirm", json={"token": token, "password": "a new password!!"},
                           headers=CSRF)
    assert response.status_code == 204
    # Existing sessions end, the token is single-use, and only the new password works.
    assert other.get("/v1/auth/me").status_code == 401
    assert client.post("/v1/auth/password-reset/confirm", json={"token": token, "password": "another password"},
                       headers=CSRF).status_code == 400
    assert client.post("/v1/auth/login", json={"email": "reset@example.com", "password": PASSWORD},
                       headers=CSRF).status_code == 401
    login(client, "reset@example.com", "a new password!!")


def test_login_is_rate_limited_per_email_and_logged(client, caplog) -> None:  # type: ignore[no-untyped-def]
    register(client, "brute@example.com")
    statuses = [
        client.post("/v1/auth/login", json={"email": "brute@example.com", "password": "nope nope nope"},
                    headers=CSRF).status_code
        for _ in range(11)
    ]
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429
    limited = client.post("/v1/auth/login", json={"email": "brute@example.com", "password": PASSWORD}, headers=CSRF)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert int(limited.headers["Retry-After"]) > 0
    assert any(getattr(record, "event", "") == "rate_limit.exceeded" for record in caplog.records)


def test_signup_and_reset_are_rate_limited_per_email(client) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        assert signup(client, "spam@example.com").status_code == 202
    assert signup(client, "spam@example.com").status_code == 429
    for _ in range(3):
        client.post("/v1/auth/password-reset/request", json={"email": "x@example.com"}, headers=CSRF)
    assert client.post("/v1/auth/password-reset/request", json={"email": "x@example.com"},
                       headers=CSRF).status_code == 429
    for _ in range(3):
        client.post("/v1/auth/resend-verification", json={"email": "y@example.com"}, headers=CSRF)
    assert client.post("/v1/auth/resend-verification", json={"email": "y@example.com"},
                       headers=CSRF).status_code == 429

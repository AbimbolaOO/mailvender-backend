"""Test setup: a real PostgreSQL database (TEST_DATABASE_URL), migrated with Alembic.

Infrastructure that would touch the network – S3, DNS and SMTP – is replaced by
in-memory fakes through the same interfaces the services use.
"""

import io
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

os.environ.setdefault(
    "TEST_DATABASE_URL", "postgresql+psycopg://mailvender:mailvender@localhost:5434/mailvender_test"
)
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["FEEDBACK_LOOP_SECRETS"] = "examplefbl:fbl-secret"
os.environ["INTERNAL_API_TOKEN"] = "internal-test-token"
os.environ["PUBLIC_API_BASE_URL"] = "https://app.test/api"
os.environ["APP_BASE_URL"] = "https://app.test"
os.environ["AWS_S3_PUBLIC_BASE_URL"] = "https://cdn.test"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import models as m  # noqa: E402
from app.api import deps  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.repositories.sql import SqlUnitOfWork  # noqa: E402
from app.services.dns import DnsLookupError  # noqa: E402
from app.services.mta import TemporaryDeliveryError  # noqa: E402
from app.services.storage import ObjectInfo, PresignedUpload  # noqa: E402
from app.worker import Worker  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(__file__))
CSRF = {"X-Requested-With": "mailvender"}


def alembic_config() -> Config:
    config = Config(os.path.join(BACKEND, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(BACKEND, "alembic"))
    config.attributes["database_url"] = os.environ["TEST_DATABASE_URL"]
    return config


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    command.upgrade(alembic_config(), "head")
    yield


TABLES = [table.name for table in reversed(m.Base.metadata.sorted_tables)]


@pytest.fixture(autouse=True)
def clean_tables() -> Iterator[None]:
    yield
    session = get_session_factory()()
    session.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    session.commit()
    session.close()


@contextmanager
def sql_uow() -> Iterator[SqlUnitOfWork]:
    session = get_session_factory()()
    try:
        yield SqlUnitOfWork(session)
    finally:
        session.close()


@pytest.fixture
def db() -> Iterator[SqlUnitOfWork]:
    with sql_uow() as uow:
        yield uow


# ---- Fakes ----


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.presigned: list[dict[str, object]] = []

    def presign_upload(self, key: str, content_type: str, max_bytes: int, expires_in: int) -> PresignedUpload:
        self.presigned.append({"key": key, "content_type": content_type, "max_bytes": max_bytes,
                               "expires_in": expires_in})
        return PresignedUpload(url="https://s3.test/bucket", fields={"key": key, "Content-Type": content_type,
                                                                     "policy": "p", "x-amz-signature": "s"},
                               expires_in=expires_in)

    def head(self, key: str) -> ObjectInfo | None:
        if key not in self.objects:
            return None
        body, content_type = self.objects[key]
        return ObjectInfo(size=len(body), content_type=content_type)

    def read(self, key: str, max_bytes: int) -> bytes:
        return self.objects[key][0][:max_bytes]

    def put(self, key: str, body: bytes, content_type: str, *, cache_control: str | None = None) -> None:
        self.objects[key] = (body, content_type)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def public_url(self, key: str) -> str:
        return f"https://cdn.test/{key}"


@dataclass
class FakeResolver:
    txt_records: dict[str, list[str]] = field(default_factory=dict)
    mx_records: dict[str, list[str]] = field(default_factory=dict)
    failing: bool = False

    def txt(self, name: str) -> list[str]:
        if self.failing:
            raise DnsLookupError("timeout")
        return list(self.txt_records.get(name, []))

    def mx(self, name: str) -> list[str]:
        if self.failing:
            raise DnsLookupError("timeout")
        return list(self.mx_records.get(name, []))


@dataclass
class FakeTransport:
    sent: list[tuple[str, list[str], bytes]] = field(default_factory=list)
    failures: list[Exception] = field(default_factory=list)

    def send(self, envelope_from: str, recipients: list[str], message: bytes) -> str:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((envelope_from, recipients, message))
        return "250 2.0.0 Ok: queued"

    def fail_temporarily(self, times: int) -> None:
        self.failures = [TemporaryDeliveryError("451 4.3.0 try again", 451) for _ in range(times)]


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver()


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def client(storage: FakeStorage, resolver: FakeResolver) -> Iterator[TestClient]:
    app.dependency_overrides[deps.get_storage] = lambda: storage
    app.dependency_overrides[deps.get_resolver] = lambda: resolver
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def worker(transport: FakeTransport, storage: FakeStorage, resolver: FakeResolver) -> Worker:
    return Worker(sql_uow, transport, storage, resolver)


# ---- Helpers ----


def last_link_token(email: str, template: str) -> str:
    with sql_uow() as uow:
        rows = uow.session.query(m.OutboxEvent).filter(m.OutboxEvent.kind == "system_email").order_by(
            m.OutboxEvent.created_at.desc()).all()
    for row in rows:
        if row.payload.get("to") == email and row.payload.get("template") == template:
            return row.payload["text"].split("token=", 1)[1].split()[0]
    raise AssertionError(f"No {template} email for {email}")


@dataclass
class Session:
    """A signed-in browser: its own cookie jar, the active account in headers."""

    client: TestClient
    email: str
    user_id: str
    account_id: str

    @property
    def headers(self) -> dict[str, str]:
        return {**CSRF, "X-Account-Id": self.account_id}

    def get(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.client.get(url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs)

    def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.client.post(url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs)

    def put(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.client.put(url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs)

    def patch(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.client.patch(url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs)

    def delete(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        return self.client.delete(url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs)


PASSWORD = "correct horse battery"


def register(client: TestClient, email: str, password: str = PASSWORD) -> None:
    response = client.post("/v1/auth/signup", json={"email": email, "password": password}, headers=CSRF)
    assert response.status_code == 202, response.text
    token = last_link_token(email.lower(), "verify_email")
    response = client.post("/v1/auth/verify-email", json={"token": token}, headers=CSRF)
    assert response.status_code == 200, response.text
    # Treat signup emails as delivered so tests only see the mail they send.
    with sql_uow() as uow:
        uow.session.query(m.OutboxEvent).filter(m.OutboxEvent.kind == "system_email").update({"status": "done"})
        uow.commit()


def login(client: TestClient, email: str, password: str = PASSWORD) -> Session:
    response = client.post("/v1/auth/login", json={"email": email, "password": password}, headers=CSRF)
    assert response.status_code == 200, response.text
    body = response.json()
    return Session(client, email, body["user"]["id"], body["user"]["default_account_id"])


@pytest.fixture
def make_user(client: TestClient):  # type: ignore[no-untyped-def]
    """Creates a verified user and returns a signed-in Session with its own cookie jar."""
    from app.main import app as application

    created: list[TestClient] = []

    def factory(email: str | None = None) -> Session:
        email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
        own = TestClient(application, base_url="https://testserver")
        created.append(own)
        register(own, email)
        return login(own, email)

    yield factory
    for c in created:
        c.close()


def png_bytes(width: int = 4, height: int = 3) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, "PNG")
    return buffer.getvalue()


# ---- Domain setup ----


def publish_dns(resolver: FakeResolver, domain: dict, *, dmarc: str | None = "v=DMARC1; p=none") -> None:
    for record in domain["records"]:
        if record["type"] == "MX":
            resolver.mx_records[record["name"]] = [record["value"].split()[1]]
        elif record["key"] == "dmarc":
            if dmarc:
                resolver.txt_records[record["name"]] = [dmarc]
        else:
            resolver.txt_records.setdefault(record["name"], []).append(record["value"])


def verified_sender(session: Session, resolver: FakeResolver, domain_name: str | None = None,
                    *, dmarc: str | None = "v=DMARC1; p=none", address: str = "news") -> dict:
    """Registers and verifies a domain and an approved sender identity; returns {domain, identity}."""
    name = domain_name or f"d{uuid.uuid4().hex[:8]}.example"
    response = session.post("/v1/domains", json={"name": name})
    assert response.status_code == 201, response.text
    domain = response.json()
    publish_dns(resolver, domain, dmarc=dmarc)
    response = session.post(f"/v1/domains/{domain['id']}/verify")
    assert response.status_code == 200 and response.json()["status"] == "verified", response.text
    domain = response.json()
    response = session.post("/v1/sender-identities", json={"domain_id": domain["id"], "email": f"{address}@{name}",
                                                           "display_name": "News"})
    assert response.status_code == 201, response.text
    identity = response.json()
    response = session.post(f"/v1/sender-identities/{identity['id']}/verify")
    assert response.status_code == 200, response.text
    return {"domain": domain, "identity": response.json()}


def send(session: Session, sender: str, to: list[str], *, key: str | None = None, **extra) -> object:  # type: ignore[no-untyped-def]
    body = {"from": sender, "to": to, "subject": "Hello", "html": "<p>Hello there, friend.</p>", **extra}
    return session.post("/v1/messages", json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex})

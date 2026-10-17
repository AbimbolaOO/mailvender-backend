import uuid
from datetime import timedelta

import pytest

from app import cli
from app import models as m
from app.errors import Conflict
from app.security import utcnow
from app.services.lifecycle import LifecycleService
from app.services.platform_dkim import PlatformDkimService
from app.worker import sync_dkim_keys
from tests.conftest import verified_sender

# Tests use the default SYSTEM_FROM_EMAIL, no-reply@mailvender.localhost.
PLATFORM = "mailvender.localhost"


def publish(resolver, view) -> None:  # type: ignore[no-untyped-def]
    resolver.txt_records[view.record_name] = [view.record_value]


@pytest.mark.parametrize("name", ["mailvender.localhost", "MAIL.Mailvender.localhost."])
def test_customers_cant_add_the_platform_domain(make_user, name) -> None:  # type: ignore[no-untyped-def]
    response = make_user().post("/v1/domains", json={"name": name})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "domain_unavailable"


def test_first_key_activates_once_published_and_rotates(db, resolver) -> None:  # type: ignore[no-untyped-def]
    service = PlatformDkimService(db, resolver)
    [view] = service.check()
    assert view.key.status == "pending" and view.dns_status == "missing"
    assert view.record_name == f"{view.key.selector}._domainkey.{PLATFORM}"
    assert view.record_value.startswith("v=DKIM1; k=rsa; p=")
    assert "PRIVATE KEY" not in view.key.private_key_encrypted
    assert [v.key.id for v in service.check()] == [view.key.id]  # no second key while one is pending

    publish(resolver, view)
    [active] = service.check()
    assert active.key.status == "active" and active.dns_status == "verified"

    nxt = service.rotate()
    with pytest.raises(Conflict):
        service.rotate()
    views = service.check()
    assert [(v.key.status, v.dns_status) for v in views] == [("active", "verified"), ("pending", "missing")]

    publish(resolver, nxt)
    [current] = service.check()
    assert current.key.id == nxt.key.id and current.key.status == "active"
    statuses = sorted(k.status for k in db.platform_dkim_keys.list_for_domain(PLATFORM))
    assert statuses == ["active", "retired"]


def test_rotate_needs_an_active_key(db, resolver) -> None:  # type: ignore[no-untyped-def]
    service = PlatformDkimService(db, resolver)
    with pytest.raises(Conflict):
        service.rotate()
    service.check()
    with pytest.raises(Conflict):
        service.rotate()


def test_signing_table_has_verified_domains_and_the_platform_key(  # type: ignore[no-untyped-def]
        make_user, resolver, db, tmp_path) -> None:
    user = make_user()
    verified = verified_sender(user, resolver, "signed.example")["domain"]
    assert user.post("/v1/domains", json={"name": "unverified.example"}).status_code == 201
    # A row for the platform domain added before it was reserved must not take over its signing.
    legacy = db.domains.add(m.Domain(account_id=uuid.UUID(user.account_id), name=PLATFORM, status="verified",
                                     return_path_host=f"bounce.{PLATFORM}", record_status={}))
    db.dkim_keys.add(m.DkimKey(account_id=legacy.account_id, domain_id=legacy.id, selector="mvlegacy",
                               private_key_encrypted=db.dkim_keys.list_for_domain(
                                   uuid.UUID(verified["id"]))[0].private_key_encrypted,
                               public_key="x", status="active"))
    db.commit()

    service = PlatformDkimService(db, resolver)
    assert sync_dkim_keys(db, tmp_path) == 1  # the platform key isn't active yet
    publish(resolver, service.check()[0])
    platform = service.check()[0].key

    assert sync_dkim_keys(db, tmp_path) == 2
    signing = (tmp_path / "SigningTable").read_text().splitlines()
    assert signing == [
        f"*@{PLATFORM} {platform.selector}._domainkey.{PLATFORM}",
        f"*@signed.example {verified['dkim_selector']}._domainkey.signed.example",
    ]
    assert f"{PLATFORM}:{platform.selector}:" in (tmp_path / "KeyTable").read_text()
    assert sorted(p.name for p in (tmp_path / "keys").iterdir()) == sorted(
        [f"{platform.selector}.private", f"{verified['dkim_selector']}.private"])
    assert "PRIVATE KEY" in (tmp_path / "keys" / f"{platform.selector}.private").read_text()


def test_retention_deletes_old_retired_platform_keys(db, resolver, storage) -> None:  # type: ignore[no-untyped-def]
    service = PlatformDkimService(db, resolver)
    publish(resolver, service.check()[0])
    service.check()
    publish(resolver, service.rotate())
    service.check()
    [old] = [k for k in db.platform_dkim_keys.list_for_domain(PLATFORM) if k.status == "retired"]
    old.retired_at = utcnow() - timedelta(days=31)
    db.commit()
    assert LifecycleService(db, storage).enforce_retention().dkim_keys_deleted == 1
    assert [k.status for k in db.platform_dkim_keys.list_for_domain(PLATFORM)] == ["active"]


def test_cli_prints_the_record_to_publish(monkeypatch, resolver, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.services.dns.SystemDnsResolver", lambda: resolver)
    assert cli.main(["platform-dkim"]) == 0
    out = capsys.readouterr().out
    assert f"Platform DKIM for {PLATFORM}" in out and "pending" in out and "NOT FOUND in DNS" in out
    assert f"._domainkey.{PLATFORM}" in out and "v=DKIM1; k=rsa; p=" in out
    assert cli.main(["platform-dkim", "rotate"]) == 1  # no active key yet

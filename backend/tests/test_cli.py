import pytest

from app.cli import upload_backup, upload_log
from app.config import get_settings


def test_upload_log_stores_the_file_under_the_logs_folder(storage) -> None:  # type: ignore[no-untyped-def]
    key = upload_log(storage, "api/2026/10/17/api-20261017T140000Z.log.gz", b"\x1f\x8b gzip")
    assert key == f"{get_settings().aws_s3_prefix}logs/api/2026/10/17/api-20261017T140000Z.log.gz"
    assert storage.objects[key] == (b"\x1f\x8b gzip", "application/gzip")


@pytest.mark.parametrize("key", ["../images/x.log.gz", "api/2026/10/17/../../x.log.gz", "api/today/x.log.gz", "api/2026/10/17/x.txt"])
def test_upload_log_rejects_other_keys(storage, key) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        upload_log(storage, key, b"")
    assert not storage.objects


def test_upload_backup_stores_encrypted_dumps_under_the_backups_folder(storage) -> None:  # type: ignore[no-untyped-def]
    body = b"age-encryption.org/v1\n-> X25519 ..."
    key = upload_backup(storage, "db/2026/10/18/mailvender-20261018T061500Z.dump.age", body)
    assert key == f"{get_settings().aws_s3_prefix}backups/db/2026/10/18/mailvender-20261018T061500Z.dump.age"
    assert storage.objects[key] == (body, "application/octet-stream")


@pytest.mark.parametrize("key", ["db/2026/10/18/mailvender-20261018T061500Z.dump", "../logs/x.dump.age",
                                 "db/2026/10/18/../../x.dump.age", "images/2026/10/18/mailvender-20261018T061500Z.dump.age"])
def test_upload_backup_rejects_other_keys(storage, key) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        upload_backup(storage, key, b"age-encryption.org/v1\n")
    assert not storage.objects


def test_upload_backup_refuses_unencrypted_dumps(storage) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="age-encrypted"):
        upload_backup(storage, "db/2026/10/18/mailvender-20261018T061500Z.dump.age", b"PGDMP\x01\x0e")
    assert not storage.objects

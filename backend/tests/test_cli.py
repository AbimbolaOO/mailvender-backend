import pytest

from app.cli import upload_log
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

"""Operational commands.

    python -m app.cli make-operator someone@example.com
    python -m app.cli run-job retention|dns_recheck|account_health|dkim_key_sync
    python -m app.cli openapi > openapi.json
    python -m app.cli dev-verify-domain example.com   (ENVIRONMENT=development only)
    python -m app.cli move-dataset-content   (one-off: datasets stored in Postgres → S3)
    python -m app.cli upload-log <service>/<YYYY>/<MM>/<DD>/<name>.log.gz < file   (infra/log-backup)
"""

import json
import re
import sys

from app.repositories.sql import SqlUnitOfWork
from app.services.storage import ObjectStorage

LOG_KEY = re.compile(r"[a-z0-9-]+/\d{4}/\d{2}/\d{2}/[A-Za-z0-9._-]+\.log\.gz")


def upload_log(storage: ObjectStorage, key: str, body: bytes) -> str:
    """Store a gzipped container log at <prefix>logs/<key> (private, like everything but public images)."""
    from app.config import get_settings

    if not LOG_KEY.fullmatch(key):
        raise ValueError(f"invalid log key: {key}")
    object_key = f"{get_settings().aws_s3_prefix}logs/{key}"
    storage.put(object_key, body, "application/gzip")
    return object_key


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, *args = argv
    if command == "openapi":
        from app.main import app

        print(json.dumps(app.openapi(), indent=2))
        return 0
    if command == "upload-log" and len(args) == 1:
        from app.services.storage import S3Storage

        try:
            print(upload_log(S3Storage(), args[0], sys.stdin.buffer.read()))
        except ValueError as error:
            print(error, file=sys.stderr)
            return 1
        return 0

    from app.db import get_session_factory

    session = get_session_factory()()
    uow = SqlUnitOfWork(session)
    try:
        if command == "make-operator" and len(args) == 1:
            user = uow.users.get_by_email(args[0].strip().lower())
            if user is None:
                print("No such user.", file=sys.stderr)
                return 1
            user.is_operator = True
            uow.commit()
            print(f"{user.email} is now an operator.")
            return 0
        if command == "dev-verify-domain" and len(args) == 1:
            # Local development can't publish real DNS records; production never allows this.
            from datetime import timedelta

            from app.config import get_settings
            from app.security import utcnow

            if get_settings().environment != "development":
                print("Only available with ENVIRONMENT=development.", file=sys.stderr)
                return 1
            domain = uow.domains.get_live_by_name(args[0].strip().lower())
            if domain is None:
                print("No such domain.", file=sys.stderr)
                return 1
            domain.status, domain.verified_at, domain.bulk_eligible = "verified", utcnow(), True
            domain.last_checked_at = utcnow() + timedelta(days=3650)  # skip DNS rechecks
            uow.commit()
            print(f"{domain.name} marked verified (development only).")
            return 0
        if command == "move-dataset-content" and not args:
            from app.services.storage import S3Storage
            from app.services.sync import SyncService

            total = 0
            while moved := SyncService(uow, S3Storage()).move_inline_content():
                total += moved
            print(f"Moved {total} dataset(s) to object storage.")
            return 0
        if command == "run-job" and len(args) == 1:
            from app.services.dns import SystemDnsResolver
            from app.services.mta import SmtpTransport
            from app.services.storage import S3Storage
            from app.worker import Worker, sql_uow

            worker = Worker(sql_uow, SmtpTransport(), S3Storage(), SystemDnsResolver())
            job = next((j for j in worker.jobs if j.name == args[0]), None)
            if job is None:
                print(f"Unknown job. Jobs: {', '.join(j.name for j in worker.jobs)}", file=sys.stderr)
                return 1
            worker.run_job(job)
            return 0
    finally:
        session.close()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

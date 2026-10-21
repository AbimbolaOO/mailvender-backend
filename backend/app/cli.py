"""Operational commands.

    python -m app.cli make-operator someone@example.com
    python -m app.cli run-job retention|dns_recheck|platform_dkim|account_health|dkim_key_sync
    python -m app.cli platform-dkim          (DKIM for SYSTEM_FROM_EMAIL's domain: shows the DNS record to publish)
    python -m app.cli platform-dkim rotate   (starts a key rotation)
    python -m app.cli openapi > openapi.json
    python -m app.cli openapi --admin > openapi-admin.json   (the admin API, for mailvender-admin)
    python -m app.cli create-admin you@example.com   (prints a temporary password once)
    python -m app.cli reset-admin you@example.com    (new temporary password, two-factor cleared, signed out)
    python -m app.cli disable-admin you@example.com
    python -m app.cli list-admins
    python -m app.cli dev-verify-domain example.com   (ENVIRONMENT=development only)
    python -m app.cli sync-templates [folder]   (starter templates: folder → database; the API runs it on start)
    python -m app.cli move-dataset-content   (one-off: datasets stored in Postgres → S3)
    python -m app.cli upload-log <service>/<YYYY>/<MM>/<DD>/<name>.log.gz < file   (infra/log-backup)
    python -m app.cli upload-backup db/<YYYY>/<MM>/<DD>/mailvender-<time>.dump.age < file   (infra/db-backup)
"""

import json
import re
import sys

from app.repositories.sql import SqlUnitOfWork
from app.services.storage import ObjectStorage

LOG_KEY = re.compile(r"[a-z0-9-]+/\d{4}/\d{2}/\d{2}/[A-Za-z0-9._-]+\.log\.gz")
BACKUP_KEY = re.compile(r"db/\d{4}/\d{2}/\d{2}/mailvender-\d{8}T\d{6}Z\.dump\.age")


def upload_log(storage: ObjectStorage, key: str, body: bytes) -> str:
    """Store a gzipped container log at <prefix>logs/<key> (private, like everything but public images)."""
    from app.config import get_settings

    if not LOG_KEY.fullmatch(key):
        raise ValueError(f"invalid log key: {key}")
    object_key = f"{get_settings().aws_s3_prefix}logs/{key}"
    storage.put(object_key, body, "application/gzip")
    return object_key


def upload_backup(storage: ObjectStorage, key: str, body: bytes) -> str:
    """Store an encrypted database dump at <prefix>backups/<key> (private)."""
    from app.config import get_settings

    if not BACKUP_KEY.fullmatch(key):
        raise ValueError(f"invalid backup key: {key}")
    if not body.startswith(b"age-encryption.org/v1"):
        raise ValueError("refusing to upload a backup that isn't age-encrypted")
    object_key = f"{get_settings().aws_s3_prefix}backups/{key}"
    storage.put(object_key, body, "application/octet-stream")
    return object_key


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, *args = argv
    if command == "openapi" and args in ([], ["--admin"]):
        from app.main import admin_openapi, app

        print(json.dumps(admin_openapi(app) if args else app.openapi(), indent=2))
        return 0
    if command in ("upload-log", "upload-backup") and len(args) == 1:
        from app.services.storage import S3Storage

        upload = upload_log if command == "upload-log" else upload_backup
        try:
            print(upload(S3Storage(), args[0], sys.stdin.buffer.read()))
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
        if command in ("create-admin", "reset-admin", "disable-admin") and len(args) == 1:
            from app.errors import ApiError
            from app.services.admin_auth import AdminAuthService

            service = AdminAuthService(uow)
            try:
                if command == "create-admin":
                    admin, password = service.create(args[0])
                    print(f"Admin {admin.email} created.\n\nTemporary password (shown once):\n\n    {password}\n\n"
                          "Sign in to the admin app with it; you'll be asked to choose a new password and to set "
                          "up an authenticator app.")
                elif command == "reset-admin":
                    password = service.reset(args[0])
                    print(f"Reset. Two-factor is cleared and every session ended.\n\nTemporary password "
                          f"(shown once):\n\n    {password}\n")
                else:
                    service.disable(args[0])
                    print("Admin disabled and signed out everywhere.")
            except ApiError as error:
                print(error.message, file=sys.stderr)
                return 1
            return 0
        if command == "list-admins" and not args:
            for admin in uow.admins.list_all():
                state = "disabled" if admin.disabled_at else ("2FA on" if admin.totp_enabled_at else "setup pending")
                print(f"{admin.email:40} {state:14} last sign-in: {admin.last_login_at or 'never'}")
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
        if command == "platform-dkim" and args in ([], ["rotate"]):
            from app.errors import Conflict
            from app.services.dns import SystemDnsResolver
            from app.services.platform_dkim import PlatformDkimService

            service = PlatformDkimService(uow, SystemDnsResolver())
            if args:
                try:
                    service.rotate()
                except Conflict as error:
                    print(error.message, file=sys.stderr)
                    return 1
            print(f"Platform DKIM for {service.domain}\n")
            for view in service.check():
                state = {"verified": "published", "missing": "NOT FOUND in DNS", "mismatched": "WRONG value in DNS",
                         "unchecked": "DNS lookup failed"}[view.dns_status]
                print(f"{view.key.status:8} {view.key.selector}   {state}")
                print(f"  TXT  {view.record_name}")
                print(f"       {view.record_value}\n")
            print("Publish each record above (TXT, exactly as shown). A pending key becomes active once its\n"
                  "record is found; the worker checks every 5 minutes, or run this command again.")
            return 0
        if command == "sync-templates" and len(args) <= 1:
            from pathlib import Path

            from app.services.templates import FOLDER, TemplateService, read_folder

            try:
                result = TemplateService(uow).sync(read_folder(Path(args[0]) if args else FOLDER))
            except ValueError as error:
                print(f"Templates not synced: {error}", file=sys.stderr)
                return 1
            print(json.dumps({"event": "templates.sync", "added": result.added, "updated": result.updated,
                              "removed": result.removed, "unchanged": result.unchanged}))
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

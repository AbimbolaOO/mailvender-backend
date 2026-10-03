"""Background worker: consumes the outbox and runs scheduled jobs.

    python -m app.worker

Delivery is at-least-once: if the process dies after Postfix accepted a message
but before the outbox commit, the message is sent again on restart.
"""

import logging
import os
import signal
import time
import traceback
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from app import models as m
from app.config import get_settings
from app.db import get_session_factory
from app.logging import configure_logging, get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.repositories.sql import SqlUnitOfWork
from app.security import decrypt_secret, utcnow
from app.services.common import UnitOfWorkFactory
from app.services.dns import DnsResolver, SystemDnsResolver
from app.services.domains import DomainService
from app.services.health import HealthService
from app.services.lifecycle import LifecycleService
from app.services.mta import MailTransport, PermanentDeliveryError, SmtpTransport, TemporaryDeliveryError
from app.services.sending import DeliveryService, system_email
from app.services.storage import ObjectStorage, S3Storage

log = get_logger("worker")

MAX_EVENT_ATTEMPTS = 10


@contextmanager
def sql_uow():
    session = get_session_factory()()
    try:
        yield SqlUnitOfWork(session)
    finally:
        session.close()


@dataclass
class Job:
    name: str
    every: timedelta
    run: Callable[[UnitOfWork], object]
    last_run: float = field(default=0.0)


class Worker:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        transport: MailTransport,
        storage: ObjectStorage,
        resolver: DnsResolver,
    ):
        self.uow_factory = uow_factory
        self.transport = transport
        self.storage = storage
        self.resolver = resolver
        self.settings = get_settings()
        self.stopping = False
        self.jobs = [
            Job("dns_recheck", timedelta(minutes=10), lambda uow: DomainService(uow, self.resolver).recheck_due()),
            Job("account_health", timedelta(minutes=5), lambda uow: HealthService(uow).evaluate_all()),
            Job("retention", timedelta(hours=24), lambda uow: LifecycleService(uow, self.storage).enforce_retention()),
        ]
        if self.settings.dkim_keys_dir:
            self.jobs.append(Job("dkim_key_sync", timedelta(seconds=30), lambda uow: sync_dkim_keys(
                uow, Path(self.settings.dkim_keys_dir))))

    # ---- Outbox ----

    def process_outbox(self, batch: int = 20) -> int:
        with self.uow_factory() as uow:
            events = uow.outbox.claim(utcnow(), batch)
            for event in events:
                try:
                    with uow.savepoint():
                        self.handle(uow, event)
                except Exception as error:  # noqa: BLE001 - one bad event must not stop the rest
                    event.attempts += 1
                    event.last_error = "".join(traceback.format_exception_only(error))[:2000]
                    if event.attempts >= MAX_EVENT_ATTEMPTS:
                        event.status = "dead"
                        log_event(log, "outbox.dead_letter", logging.ERROR, event_id=str(event.id), kind=event.kind)
                    else:
                        event.available_at = utcnow() + timedelta(seconds=30 * 2 ** event.attempts)
                        log_event(log, "outbox.event_failed", logging.WARNING, event_id=str(event.id),
                                  kind=event.kind, error=event.last_error)
                if event.status in ("done", "dead"):
                    event.processed_at = utcnow()
            uow.commit()
            return len(events)

    def handle(self, uow: UnitOfWork, event: m.OutboxEvent) -> None:
        if event.kind == "deliver_recipient":
            DeliveryService(uow, self.transport).deliver(event)
        elif event.kind == "system_email":
            self.send_system_email(event)
        elif event.kind == "account_export":
            LifecycleService(uow, self.storage).build_export(uuid.UUID(event.payload["export_id"]))
            event.status = "done"
        elif event.kind == "account_assets_delete":
            LifecycleService(uow, self.storage).delete_account_assets(uuid.UUID(event.payload["account_id"]))
            event.status = "done"
        else:
            log_event(log, "outbox.unknown_kind", logging.ERROR, kind=event.kind)
            event.status = "dead"

    def send_system_email(self, event: m.OutboxEvent) -> None:
        payload = event.payload
        mime = system_email(payload)
        sender = self.settings.system_from_email.rsplit("<", 1)[-1].strip(">").strip()
        try:
            self.transport.send(sender, [payload["to"]], mime.as_bytes())
        except PermanentDeliveryError as error:
            log_event(log, "system_email.failed", logging.WARNING, template=payload.get("template"), error=str(error))
        except TemporaryDeliveryError as error:
            event.attempts += 1
            if event.attempts < MAX_EVENT_ATTEMPTS:
                event.available_at = utcnow() + timedelta(seconds=30 * 2 ** event.attempts)
                event.last_error = str(error)[:1000]
                return
        # The body holds a live token – don't keep it once sent.
        event.payload = {"to": payload["to"], "template": payload.get("template")}
        event.status = "done"
        log_event(log, "system_email.sent", template=payload.get("template"))

    # ---- Scheduled jobs ----

    def run_due_jobs(self) -> None:
        now = time.monotonic()
        for job in self.jobs:
            if now - job.last_run < job.every.total_seconds():
                continue
            job.last_run = now
            self.run_job(job)

    def run_job(self, job: Job) -> None:
        started = utcnow()
        error: str | None = None
        with self.uow_factory() as uow:
            if not uow.jobs.try_lock(job.name):
                return  # another worker runs it
            try:
                result = job.run(uow)
                log_event(log, f"job.{job.name}.completed", result=str(result))
            except Exception as exc:  # noqa: BLE001
                uow.rollback()
                error = "".join(traceback.format_exception_only(exc))[:2000]
                log_event(log, f"job.{job.name}.failed", logging.ERROR, error=error)
            uow.jobs.record(job.name, started, utcnow(), error)
            uow.commit()

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stopping", True))
        log_event(log, "worker.started")
        while not self.stopping:
            try:
                processed = self.process_outbox()
                self.run_due_jobs()
            except Exception:  # noqa: BLE001
                log.exception("worker loop error", extra={"event": "worker.loop_error"})
                processed = 0
            if not processed:
                time.sleep(1)
        log_event(log, "worker.stopped")


def sync_dkim_keys(uow: UnitOfWork, directory: Path) -> int:
    """Writes the active DKIM keys as OpenDKIM KeyTable/SigningTable files.

    The private keys are decrypted only into this directory, which must be a
    private volume shared with the OpenDKIM container alone. OpenDKIM reloads
    when the `generation` file changes (see infra/opendkim/entrypoint.sh).
    """
    directory.mkdir(parents=True, exist_ok=True)
    keys_dir = directory / "keys"
    keys_dir.mkdir(exist_ok=True)
    key_table, signing_table = [], []
    wanted = set()
    for key, domain in sorted(uow.dkim_keys.list_signing(), key=lambda pair: pair[1].name):
        filename = f"{key.selector}.private"
        wanted.add(filename)
        path = keys_dir / filename
        if not path.exists():
            path.write_text(decrypt_secret(key.private_key_encrypted))
            os.chmod(path, 0o600)
        key_table.append(f"{key.selector}._domainkey.{domain.name} {domain.name}:{key.selector}:{path}")
        signing_table.append(f"*@{domain.name} {key.selector}._domainkey.{domain.name}")
    for stale in keys_dir.glob("*.private"):
        if stale.name not in wanted:
            stale.unlink()
    changed = False
    for name, lines in (("KeyTable", key_table), ("SigningTable", signing_table)):
        content = "\n".join(lines) + "\n"
        target = directory / name
        if not target.exists() or target.read_text() != content:
            tmp = directory / f".{name}.tmp"
            tmp.write_text(content)
            tmp.replace(target)
            changed = True
    if changed:
        (directory / "generation").write_text(str(time.time()))
        log_event(log, "dkim.keys_synced", keys=len(key_table))
    return len(key_table)


def main() -> None:
    configure_logging()
    Worker(sql_uow, SmtpTransport(), S3Storage(), SystemDnsResolver()).run_forever()


if __name__ == "__main__":
    main()

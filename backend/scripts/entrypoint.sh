#!/bin/sh
# api:    apply migrations, sync the starter templates, then serve (traffic only after migrations succeed)
# worker: wait for migrations to be current, then consume the outbox
set -e

case "${1:-api}" in
  api)
    alembic upgrade head
    # A broken template file mustn't keep the API down: the previous templates stay served.
    python -m app.cli sync-templates || echo '{"event": "templates.sync_failed"}'
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --no-access-log
    ;;
  worker)
    until alembic current 2>/dev/null | grep -q '(head)'; do
      echo '{"event": "worker.waiting_for_migrations"}'
      sleep 2
    done
    exec python -m app.worker
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    exec "$@"
    ;;
esac

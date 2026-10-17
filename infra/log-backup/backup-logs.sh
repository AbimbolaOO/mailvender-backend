#!/bin/sh
# Copies the production containers' logs to S3, one gzip file per service and run:
#   s3://<AWS_S3_BUCKET>/prod/logs/<service>/<YYYY>/<MM>/<DD>/<service>-<until>.log.gz
# Docker keeps only 250 MB per service (docker-compose.prod.yml), so this runs every
# hour from mailvender-log-backup.timer. Upload goes through the API image
# (`python -m app.cli upload-log`), which already has the AWS credentials from .env.
# A failed run leaves the state file alone, so the next run picks up the same window.
set -eu

cd "$(dirname "$0")/../.."
compose="docker compose -f docker-compose.prod.yml"
state=/var/lib/mailvender/log-backup-since
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
mkdir -p "$(dirname "$state")"

until=$(date -u +%Y-%m-%dT%H:%M:%SZ)
since=$(cat "$state" 2>/dev/null || true) # empty on the first run: everything Docker still has
day=$(date -u -d "$until" +%Y/%m/%d)
stamp=$(date -u -d "$until" +%Y%m%dT%H%M%SZ)

for service in api worker postfix opendkim; do
  $compose logs --no-color --no-log-prefix --timestamps ${since:+--since "$since"} --until "$until" "$service" > "$tmp"
  [ -s "$tmp" ] || continue
  gzip -c "$tmp" | $compose run --rm --no-deps -T api python -m app.cli upload-log "$service/$day/$service-$stamp.log.gz"
done

echo "$until" > "$state"

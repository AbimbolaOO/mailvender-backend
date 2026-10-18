#!/bin/bash
# Backs up the production database to S3, encrypted:
#   s3://<AWS_S3_BUCKET>/prod/backups/db/<YYYY>/<MM>/<DD>/mailvender-<time>.dump.age
# Runs every 6 hours from mailvender-db-backup.timer.
#
# * pg_dump (custom format: compressed, restorable table by table) of the `public`
#   schema only – everything Mailvender owns; Supabase's own schemas are left out.
#   It runs from the postgres:17 image, so the host needs no PostgreSQL client.
# * The dump is encrypted with age to the public key in /etc/mailvender/backup-recipient.txt.
#   The private key is NOT on this server: a stolen server or bucket can't read backups.
# * Nothing unencrypted touches the disk, and only a complete dump is uploaded.
# Restore: docs/PRODUCTION_SETUP.md "Restoring the database".
set -euo pipefail

cd "$(dirname "$0")/../.."
compose="docker compose -f docker-compose.prod.yml"
recipient=/etc/mailvender/backup-recipient.txt
state=/var/lib/mailvender/db-backup-last
mkdir -p "$(dirname "$state")"

[ -s "$recipient" ] || { echo "missing $recipient (the age public key, age1…)" >&2; exit 1; }
command -v age >/dev/null || { echo "age isn't installed: apt install -y age" >&2; exit 1; }

tmp=$(mktemp /var/lib/mailvender/db-backup.XXXXXX)
trap 'rm -f "$tmp"' EXIT

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
key="db/$(date -u -d "$now" +%Y/%m/%d)/mailvender-$(date -u -d "$now" +%Y%m%dT%H%M%SZ).dump.age"

# libpq wants postgresql://, not SQLAlchemy's postgresql+psycopg://.
DBURL=$(sed -n 's/^DATABASE_URL=//p' .env | sed 's#^postgresql+psycopg://#postgresql://#')
export DBURL

docker run --rm -e DBURL postgres:17-alpine \
    sh -c 'exec pg_dump --dbname="$DBURL" --format=custom --schema=public --no-owner --no-privileges' \
  | age --encrypt --recipients-file "$recipient" > "$tmp"

$compose run --rm --no-deps -T api python -m app.cli upload-backup "$key" < "$tmp"
echo "$now $(stat -c %s "$tmp") $key" > "$state"

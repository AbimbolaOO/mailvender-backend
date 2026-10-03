#!/bin/sh
# Local S3: creates the bucket and allows anonymous reads of finalized assets only.
set -e
until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do sleep 1; done
mc mb --ignore-existing "local/$BUCKET"
mc anonymous set-json /policy.json "local/$BUCKET"
echo "bucket $BUCKET ready"

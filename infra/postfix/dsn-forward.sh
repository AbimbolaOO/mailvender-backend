#!/bin/sh
# Postfix pipe: posts one DSN (stdin) to the API. Exit 75 = temporary failure, Postfix retries.
. /etc/postfix/dsn.env
curl --silent --show-error --fail --max-time 30 \
  -H "X-Internal-Token: ${INTERNAL_API_TOKEN}" \
  -H "Content-Type: message/rfc822" \
  --data-binary @- \
  "${MAILVENDER_API_URL}/internal/dsn?recipient=$(printf %s "$1" | sed 's/@/%40/')" >/dev/null || exit 75

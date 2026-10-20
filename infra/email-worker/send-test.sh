#!/bin/bash
# Posts a test email to the API like the Email Worker does (local testing, or to check production).
#   infra/email-worker/send-test.sh [postmaster|abuse|dmarc] [API URL] [domain]
# Defaults: postmaster, http://localhost:8000, mailvender.localhost. INBOUND_MAIL_SECRET must be set
# (local: dev-inbound-mail-secret, from docker-compose.yml).
set -euo pipefail
mailbox=${1:-postmaster}
api=${2:-http://localhost:8000}
domain=${3:-mailvender.localhost}
: "${INBOUND_MAIL_SECRET:?set INBOUND_MAIL_SECRET}"

body=$(printf 'From: Test Sender <tester@example.org>\r\nTo: %s@%s\r\nSubject: Test message for %s@\r\nMessage-ID: <%s@example.org>\r\nDate: %s\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello! If you can read this in the admin Inbox, the role inbox works.\r\n' \
  "$mailbox" "$domain" "$mailbox" "$(date +%s)-$RANDOM" "$(date -R 2>/dev/null || date)")
signature="sha256=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "$INBOUND_MAIL_SECRET" | sed 's/^.*= //')"
printf '%s' "$body" | curl -sS -X POST "$api/internal/inbound-mail?to=$mailbox@$domain" \
  -H 'Content-Type: message/rfc822' -H "X-Mailvender-Signature: $signature" --data-binary @-
echo

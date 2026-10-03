#!/bin/sh
# Runs OpenDKIM and reloads it (SIGUSR1) whenever the worker publishes new keys
# – that's how DKIM key rotation takes effect without a restart.
set -e
DIR=/var/lib/mailvender-dkim
mkdir -p "$DIR/keys" /run/opendkim
[ -f "$DIR/KeyTable" ] || : > "$DIR/KeyTable"
[ -f "$DIR/SigningTable" ] || : > "$DIR/SigningTable"

opendkim -f -x /etc/opendkim.conf &
PID=$!
trap 'kill -TERM $PID; wait $PID; exit 0' TERM INT

last=""
while kill -0 "$PID" 2>/dev/null; do
  current="$(cat "$DIR/generation" 2>/dev/null || true)"
  if [ -n "$current" ] && [ "$current" != "$last" ]; then
    [ -n "$last" ] && kill -USR1 "$PID" && echo '{"event": "opendkim.reloaded"}'
    last="$current"
  fi
  sleep 5
done
wait "$PID"

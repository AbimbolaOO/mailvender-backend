#!/bin/bash
# Prints the production containers' logs. Read-only.
#   On the server:  /opt/mailvender/mailvender-backend/infra/logs.sh [containers…] [options]
#   From your Mac:  ssh -t root@2.29.100.188 /opt/mailvender/mailvender-backend/infra/logs.sh api -f
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: logs.sh [containers…] [options]

Containers (default: all four):
  api                    the API (also: apis)
  worker                 sending, system emails, scheduled jobs (also: workers)
  postfix                the mail server: deliveries, bounces (also: mail, smtp)
  opendkim               DKIM signing (also: dkim)
  all                    all of the above

Options:
  -n, --lines N          last N lines per container (default 100)
  -s, --since TIME       only since TIME: 30m, 2h, 1d or 2026-10-18T09:00:00
  -f, --follow           keep printing new lines (Ctrl+C to stop)
  -e, --errors           only errors and warnings (also tracebacks, bounced/deferred mail);
                         without -n/-s it searches the last 24 hours
  -g, --grep TEXT        only lines containing TEXT (e.g. an email address or request id)
  -h, --help             this help

Examples:
  logs.sh api                     last 100 lines of the API
  logs.sh api worker -n 50        last 50 lines of each
  logs.sh api -f                  follow the API live
  logs.sh --errors --since 1d     errors from all containers in the last day
  logs.sh postfix -g gmail.com    deliveries to Gmail
EOF
}

containers=() lines="" since="" follow="" errors="" pattern=""
while [ $# -gt 0 ]; do
  case "$1" in
    api|apis) containers+=(api) ;;
    worker|workers) containers+=(worker) ;;
    postfix|mail|smtp) containers+=(postfix) ;;
    opendkim|dkim) containers+=(opendkim) ;;
    all) containers+=(api worker postfix opendkim) ;;
    -n|--lines) lines=${2:?--lines needs a number}; shift ;;
    -s|--since) since=${2:?--since needs a time}; shift ;;
    -f|--follow) follow=1 ;;
    -e|--errors) errors=1 ;;
    -g|--grep) pattern=${2:?--grep needs text}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown container or option: $1" >&2; echo >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[ ${#containers[@]} -gt 0 ] || containers=(api worker postfix opendkim)
if [ -z "$lines" ]; then
  if [ -n "$errors" ] && [ -z "$since" ]; then lines=all since=24h; else lines=100; fi
fi
[[ "$lines" =~ ^([0-9]+|all)$ ]] || { echo "--lines needs a number, got: $lines" >&2; exit 2; }
# "1d" isn't understood by docker; turn days into hours.
[[ "$since" =~ ^([0-9]+)d$ ]] && since="$((BASH_REMATCH[1] * 24))h"

cd "$(dirname "$0")/.."
args=(logs --timestamps --tail "$lines")
[ -n "$since" ] && args+=(--since "$since")
[ -n "$follow" ] && args+=(--follow)

only_errors() {
  if [ -z "$errors" ]; then cat; return; fi
  grep --line-buffered -iE \
    '"level": "(warning|error|critical)"|Traceback|Exception|status=(bounced|deferred)|warning:|error:|fatal:|panic:'
}
only_matching() {
  if [ -z "$pattern" ]; then cat; return; fi
  grep --line-buffered -iF -- "$pattern"
}

# grep exits 1 when nothing matches; that's not an error here.
docker compose -f docker-compose.prod.yml "${args[@]}" "${containers[@]}" 2>&1 | only_errors | only_matching || true

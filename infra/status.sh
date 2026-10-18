#!/bin/bash
# Prints the health of the production server in one go. Read-only.
#   On the server:  /opt/mailvender/mailvender-backend/infra/status.sh
#   From your Mac:  ssh root@2.29.100.188 /opt/mailvender/mailvender-backend/infra/status.sh
# Exits 1 if anything needs attention (listed at the end).
set -u
exec </dev/null  # docker compose exec/logs must never read this script's input

cd "$(dirname "$0")/.."
compose="docker compose -f docker-compose.prod.yml"
problems=()
ok() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
bad() { printf '  \033[31m✘\033[0m %s\n' "$1"; problems+=("$1"); }
section() { printf '\n\033[1m%s\033[0m\n' "$1"; }
age_hours() { echo $(( ($(date +%s) - $(date -d "$1" +%s)) / 3600 )); }

section "Containers"
for service in api worker postfix opendkim; do
  id=$($compose ps -q "$service" 2>/dev/null)
  if [ -z "$id" ]; then bad "$service: not running"; continue; fi
  read -r state health restarts started < <(docker inspect -f \
    '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}} {{.RestartCount}} {{.State.StartedAt}}' "$id")
  [ "$health" = - ] && health="" || health=" ($health)"
  line="$service: $state$health, up since ${started%%.*}Z, $restarts restarts"
  if [ "$state" = running ] && [ "$health" != " (unhealthy)" ] && [ "$restarts" -lt 3 ]; then ok "$line"; else bad "$line"; fi
done

section "API"
ready=$(curl -s -m 5 http://localhost:8000/health/ready)
[[ "$ready" == *'"status":"ok"'* ]] && ok "local: $ready" || bad "local health check failed: ${ready:-no answer}"
public=$(curl -s -m 10 https://api.mailvender.com/health/ready)
[[ "$public" == *'"status":"ok"'* ]] && ok "https://api.mailvender.com: ok" || bad "https://api.mailvender.com: ${public:-no answer}"
migration=$($compose exec -T api alembic current 2>/dev/null | tail -1)
[[ "$migration" == *"(head)"* ]] && ok "database migration: $migration" || bad "database migration not at head: ${migration:-unknown}"
systemctl is-active --quiet caddy && ok "caddy: running" || bad "caddy: not running"

section "Mail"
if timeout 5 bash -c '</dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then ok "outbound port 25: open"
else bad "outbound port 25: BLOCKED (Hetzner unblock, PRODUCTION_SETUP step 2)"; fi
dkim=$($compose exec -T api python -m app.cli platform-dkim 2>/dev/null | sed -n 3p | tr -s ' ')
[[ "$dkim" == "active "*" published" ]] && ok "platform DKIM: $dkim" || bad "platform DKIM: ${dkim:-unknown} (run app.cli platform-dkim)"
queue=$($compose exec -T postfix mailq 2>/dev/null | tail -1)
case "$queue" in
  *"is empty"*) ok "mail queue: empty" ;;
  *) n=$(grep -o 'in [0-9]* Request' <<<"$queue" | grep -o '[0-9]*'); [ "${n:-0}" -lt 50 ] && ok "mail queue: ${n:-?} waiting" || bad "mail queue: $n waiting" ;;
esac
[ "$(dig +short -x 2.29.100.188)" = "mail.mailvender.com." ] && ok "reverse DNS: mail.mailvender.com" || bad "reverse DNS isn't mail.mailvender.com"
for cert in certs/fullchain.pem /etc/letsencrypt/live/mail.mailvender.com/fullchain.pem; do
  [ -f "$cert" ] || continue
  end=$(openssl x509 -enddate -noout -in "$cert" | cut -d= -f2)
  days=$(( ($(date -d "$end" +%s) - $(date +%s)) / 86400 ))
  [ "$days" -ge 14 ] && ok "TLS certificate $cert: $days days left" || bad "TLS certificate $cert: expires in $days days"
done

section "Backups to S3"
if [ -f /var/lib/mailvender/log-backup-since ]; then
  last=$(cat /var/lib/mailvender/log-backup-since); h=$(age_hours "$last")
  [ "$h" -le 2 ] && ok "logs: last upload $last ($h h ago)" || bad "logs: last upload $last ($h h ago)"
else bad "logs: never uploaded (install the timer, PRODUCTION_SETUP step 14)"; fi
if [ -f /var/lib/mailvender/db-backup-last ]; then
  read -r last size key < /var/lib/mailvender/db-backup-last; h=$(age_hours "$last")
  [ "$h" -le 7 ] && ok "database: last backup $last ($h h ago, $((size / 1024)) KB) $key" \
    || bad "database: last backup $last ($h h ago)"
else bad "database: never backed up (install the timer, PRODUCTION_SETUP step 14)"; fi
for timer in mailvender-log-backup.timer mailvender-db-backup.timer; do
  systemctl is-active --quiet "$timer" && ok "$timer: active, next $(systemctl show "$timer" -p NextElapseUSecRealtime --value)" \
    || bad "$timer: not active"
  unit=${timer%.timer}.service
  [ "$(systemctl show "$unit" -p Result --value)" = success ] || bad "$unit: last run failed (journalctl -u $unit -n 30)"
done

section "Errors in the last 24 hours (api, worker)"
errors=$($compose logs --since 24h api worker 2>&1 | grep -E '"level": "(error|critical)"|Traceback')
count=$(grep -c . <<<"$errors")
if [ "$count" -eq 0 ]; then ok "none"
else bad "$count error lines"; tail -5 <<<"$errors" | cut -c1-200 | sed 's/^/    /'; fi
warnings=$($compose logs --since 24h api worker 2>&1 | grep -c '"level": "warning"')
echo "  · $warnings warnings (details: $compose logs --since 24h api worker | grep warning)"

section "Server"
disk=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[ "$disk" -lt 80 ] && ok "disk: $disk% used" || bad "disk: $disk% used"
mem=$(free -m | awk '/^Mem:/ {print $7}')
[ "$mem" -gt 200 ] && ok "memory: $mem MB available" || bad "memory: only $mem MB available"
ok "load:$(uptime | sed 's/.*load average://')"

echo
if [ ${#problems[@]} -eq 0 ]; then printf '\033[32mAll good.\033[0m\n'; exit 0; fi
printf '\033[31m%d thing(s) need attention:\033[0m\n' "${#problems[@]}"
printf '  - %s\n' "${problems[@]}"
exit 1

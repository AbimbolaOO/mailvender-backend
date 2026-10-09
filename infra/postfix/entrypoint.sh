#!/bin/sh
set -e
: "${POSTFIX_ENVIRONMENT:=development}"
: "${POSTFIX_HOSTNAME:=mail.mailvender.localhost}"
: "${POSTFIX_MYNETWORKS:?POSTFIX_MYNETWORKS is required: the address(es) of the Mailvender worker only}"
: "${POSTFIX_RELAYHOST:=}"
: "${POSTFIX_RELAYHOST_USERNAME:=}"
: "${POSTFIX_RELAYHOST_PASSWORD:=}"
: "${POSTFIX_SMTP_TLS_LEVEL:=may}"
: "${POSTFIX_TLS_CERT:=/etc/ssl/certs/ssl-cert-snakeoil.pem}"
: "${POSTFIX_TLS_KEY:=/etc/ssl/private/ssl-cert-snakeoil.key}"
: "${OPENDKIM_HOST:=opendkim}"
: "${MAILVENDER_API_URL:=http://api:8000}"
: "${INTERNAL_API_TOKEN:?INTERNAL_API_TOKEN is required}"
export POSTFIX_HOSTNAME POSTFIX_MYNETWORKS POSTFIX_RELAYHOST POSTFIX_SMTP_TLS_LEVEL POSTFIX_TLS_CERT POSTFIX_TLS_KEY OPENDKIM_HOST

fail() {
  echo "postfix: $*" >&2
  exit 1
}

# Production: refuse to start with development values instead of silently
# sending unencrypted, from a placeholder hostname, or into a test inbox.
if [ "$POSTFIX_ENVIRONMENT" = production ]; then
  case "$POSTFIX_HOSTNAME" in
    *localhost) fail "POSTFIX_HOSTNAME is a development placeholder" ;;
    *.*) ;;
    *) fail "POSTFIX_HOSTNAME must be a fully qualified name with matching PTR records" ;;
  esac
  case "$POSTFIX_RELAYHOST" in *mailpit*) fail "POSTFIX_RELAYHOST points at Mailpit" ;; esac
  [ "$POSTFIX_SMTP_TLS_LEVEL" != none ] || fail "POSTFIX_SMTP_TLS_LEVEL=none is not allowed in production"
  case "$POSTFIX_TLS_CERT" in *snakeoil*) fail "a real TLS certificate is required in production (POSTFIX_TLS_CERT / POSTFIX_TLS_KEY)" ;; esac
  [ -f "$POSTFIX_TLS_CERT" ] && [ -f "$POSTFIX_TLS_KEY" ] \
    || fail "TLS certificate not found: $POSTFIX_TLS_CERT / $POSTFIX_TLS_KEY"
  [ "$INTERNAL_API_TOKEN" != dev-internal-token ] || fail "INTERNAL_API_TOKEN is the development value"
fi

# Fill the template (only our own variables).
awk '{
  while (match($0, /\$\{[A-Z_]+\}/)) {
    name = substr($0, RSTART + 2, RLENGTH - 3)
    $0 = substr($0, 1, RSTART - 1) ENVIRON[name] substr($0, RSTART + RLENGTH)
  }
  print
}' /etc/postfix/main.cf.tmpl > /etc/postfix/main.cf

if [ ! -f "$POSTFIX_TLS_CERT" ]; then
  postconf -e smtpd_tls_security_level=none
fi

if [ -z "$POSTFIX_RELAYHOST" ]; then
  # Direct delivery to recipients' MX: throttle the big mailbox providers.
  postconf -e 'transport_maps = regexp:/etc/postfix/transport, regexp:/etc/postfix/provider_transport'
elif [ -n "$POSTFIX_RELAYHOST_USERNAME" ]; then
  # Authenticated smarthost (e.g. when the host blocks outbound port 25).
  # Credentials are only ever sent over verified TLS.
  [ -n "$POSTFIX_RELAYHOST_PASSWORD" ] || fail "POSTFIX_RELAYHOST_PASSWORD is required with POSTFIX_RELAYHOST_USERNAME"
  umask 077
  printf '%s %s:%s\n' "$POSTFIX_RELAYHOST" "$POSTFIX_RELAYHOST_USERNAME" "$POSTFIX_RELAYHOST_PASSWORD" > /etc/postfix/sasl_passwd
  postmap /etc/postfix/sasl_passwd
  umask 022
  postconf -e smtp_sasl_auth_enable=yes \
    smtp_sasl_password_maps=hash:/etc/postfix/sasl_passwd \
    smtp_sasl_security_options=noanonymous \
    smtp_sasl_tls_security_options=noanonymous \
    smtp_tls_security_level=secure \
    smtp_tls_mandatory_protocols='>=TLSv1.2'
fi

umask 027
printf 'INTERNAL_API_TOKEN=%s\nMAILVENDER_API_URL=%s\n' "$INTERNAL_API_TOKEN" "$MAILVENDER_API_URL" > /etc/postfix/dsn.env
chown root:nogroup /etc/postfix/dsn.env
chmod 0640 /etc/postfix/dsn.env

# No chroot: inside a container it only breaks Docker's DNS (service names like "opendkim").
postconf -F '*/*/chroot = n'
# The queue may live on a fresh volume (production): create/repair its directories.
postfix set-permissions >/dev/null 2>&1 || true
postfix check
exec postfix start-fg

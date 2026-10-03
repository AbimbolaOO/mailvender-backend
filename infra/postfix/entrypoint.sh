#!/bin/sh
set -e
: "${POSTFIX_HOSTNAME:=mail.mailvender.localhost}"
: "${POSTFIX_MYNETWORKS:?POSTFIX_MYNETWORKS is required: the address(es) of the Mailvender worker only}"
: "${POSTFIX_RELAYHOST:=}"
: "${POSTFIX_SMTP_TLS_LEVEL:=may}"
: "${POSTFIX_TLS_CERT:=/etc/ssl/certs/ssl-cert-snakeoil.pem}"
: "${POSTFIX_TLS_KEY:=/etc/ssl/private/ssl-cert-snakeoil.key}"
: "${OPENDKIM_HOST:=opendkim}"
: "${MAILVENDER_API_URL:=http://api:8000}"
: "${INTERNAL_API_TOKEN:?INTERNAL_API_TOKEN is required}"
export POSTFIX_HOSTNAME POSTFIX_MYNETWORKS POSTFIX_RELAYHOST POSTFIX_SMTP_TLS_LEVEL POSTFIX_TLS_CERT POSTFIX_TLS_KEY OPENDKIM_HOST

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

umask 027
printf 'INTERNAL_API_TOKEN=%s\nMAILVENDER_API_URL=%s\n' "$INTERNAL_API_TOKEN" "$MAILVENDER_API_URL" > /etc/postfix/dsn.env
chown root:nogroup /etc/postfix/dsn.env
chmod 0640 /etc/postfix/dsn.env

# No chroot: inside a container it only breaks Docker's DNS (service names like "opendkim").
postconf -F '*/*/chroot = n'
postfix check
exec postfix start-fg

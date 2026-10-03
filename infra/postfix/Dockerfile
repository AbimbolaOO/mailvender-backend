FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends postfix curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY main.cf.tmpl /etc/postfix/main.cf.tmpl
COPY master.cf.append /etc/postfix/master.cf.append
COPY relay_domains recipient_access transport /etc/postfix/
COPY dsn-forward.sh /usr/local/bin/dsn-forward
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /usr/local/bin/dsn-forward /entrypoint.sh \
    && cat /etc/postfix/master.cf.append >> /etc/postfix/master.cf

EXPOSE 25
ENTRYPOINT ["/entrypoint.sh"]

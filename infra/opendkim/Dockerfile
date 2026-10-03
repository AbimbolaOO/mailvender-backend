FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends opendkim procps \
    && rm -rf /var/lib/apt/lists/*

COPY opendkim.conf /etc/opendkim.conf
COPY TrustedHosts /etc/opendkim/TrustedHosts
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

EXPOSE 8891
ENTRYPOINT ["/entrypoint.sh"]

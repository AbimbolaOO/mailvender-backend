"""Runtime configuration, read from environment variables (see .env.example).

Secrets (database password, signing keys, AWS credentials, the DKIM encryption
key) are only ever supplied through the environment or a secret store; nothing
here has a usable production default.
"""

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development defaults that must never reach production mail.
DEV_SECRET_KEY = "dev-only-insecure-secret-key-change-me-please-0000"
DEV_DKIM_ENCRYPTION_KEY = "ZGV2LW9ubHktZGtpbS1lbmNyeXB0aW9uLWtleS0wMDA="
DEV_INTERNAL_API_TOKEN = "dev-internal-token"


class Settings(BaseSettings):
    # Blank values (e.g. optional entries left empty in .env) mean "use the default".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    environment: str = "development"
    database_url: str = "postgresql+psycopg://mailvender:mailvender@localhost:5434/mailvender"

    # Public URLs. `app_base_url` is the Next.js frontend (links in system
    # emails); `public_api_base_url` is how recipients reach this API
    # (unsubscribe and view-in-browser links), e.g. https://www.mailvender.com/api.
    app_base_url: str = "http://localhost:3000"
    public_api_base_url: str = "http://localhost:3000/api"
    # Behind the Next.js rewrite / a load balancer, take the client IP from X-Forwarded-For.
    trust_forwarded_for: bool = False
    # Shared with the frontend host (API_PROXY_SECRET there, e.g. Vercel). A request carrying it
    # comes from the frontend proxy, so its X-Mailvender-Client-Ip names the browser. Blank = off.
    api_proxy_secret: str = ""
    # Shared with the admin app's host (ADMIN_PROXY_SECRET there). /v1/admin/* answers only requests
    # carrying it; anything else gets a 404. Blank = the admin API is off.
    admin_proxy_secret: str = ""
    max_request_bytes: int = 25 * 1024 * 1024
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Secrets. Each must be at least 32 bytes of randomness in production.
    secret_key: str = DEV_SECRET_KEY
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt DKIM private keys.
    dkim_encryption_key: str = DEV_DKIM_ENCRYPTION_KEY
    # Shared secret for Postfix → API DSN delivery (`/internal/dsn`).
    internal_api_token: str = DEV_INTERNAL_API_TOKEN
    # Feedback-loop sources and their HMAC secrets: "name:secret,name2:secret2".
    feedback_loop_secrets: str = ""
    # HMAC secret of the Cloudflare Email Worker that posts postmaster@/abuse@/dmarc@ mail to
    # /internal/inbound-mail. Blank = inbound mail is refused.
    inbound_mail_secret: str = ""
    inbound_mail_max_bytes: int = 10 * 1024 * 1024
    # The platform's own sending IPs (comma-separated): DMARC report rows from any other IP are flagged.
    known_sending_ips: str = ""

    # Reputation (services/reputation.py). DNS blocklists our sending IPs (KNOWN_SENDING_IPS) and the
    # platform domain are checked against hourly, and links in customers' emails at send time. Blank
    # turns a check off. Spamhaus answers queries from big public resolvers (8.8.8.8, 1.1.1.1) with an
    # error, so the server's own resolver must do these lookups; heavy commercial use needs a Spamhaus
    # DQS key (then use <key>.zen.dq.spamhaus.net etc. here).
    blocklist_ip_zones: str = "zen.spamhaus.org,bl.spamcop.net,b.barracudacentral.org"
    blocklist_domain_zones: str = "dbl.spamhaus.org"
    link_blocklist_zones: str = "dbl.spamhaus.org"

    # Alerts (services/alerts.py): when a health check turns red (and when it's fixed). Emails go to
    # ALERT_EMAILS (comma-separated; blank = the active admins); ALERT_WEBHOOK_URL gets a Slack/
    # Discord-style {"text": …} POST too – useful because email alerts can't arrive while mail is broken.
    alert_emails: str = ""
    alert_webhook_url: str = ""
    admin_app_url: str = ""  # production: https://backoffice.mailvender.com (docker-compose.prod.yml) – linked from alerts

    # Reject passwords found in known data breaches (Have I Been Pwned "range" API: only the first five
    # characters of the password's SHA-1 leave the server). If the service can't be reached, the
    # password is allowed.
    pwned_passwords_check: bool = True

    # Webhooks (services/webhooks.py): customers' URLs that get signed event notifications.
    webhook_max_endpoints: int = 10
    webhook_timeout_seconds: float = 10.0
    # Only https URLs on the public internet. Locally, a receiver on your machine needs both true.
    webhook_allow_http: bool = False
    webhook_allow_private_addresses: bool = False
    retention_webhook_deliveries_days: int = 30

    # Email written in the admin app (see docs/ADMIN_SETUP.md). Sent from <mailbox>@<platform domain>.
    admin_mail_from_mailboxes: str = "support,postmaster,abuse"
    admin_mail_from_name: str = "Mailvender"
    admin_mail_max_recipients: int = 50
    # One upload per file stays under Vercel's 4.5 MB request limit; the total stays under Postfix's
    # 10 MB message_size_limit once base64-encoded.
    admin_mail_max_attachment_bytes: int = 4 * 1024 * 1024
    admin_mail_max_total_bytes: int = 7 * 1024 * 1024

    # Admin sessions (separate from customer sessions; see docs/ADMIN_SETUP.md)
    admin_session_idle_minutes: int = 30
    admin_session_ttl_hours: int = 8
    rate_limit_admin_login_ip: str = "5/900"
    rate_limit_admin_login_email: str = "5/900"
    admin_max_failed_logins: int = 10
    admin_lockout_minutes: int = 30
    admin_stats_cache_seconds: int = 60
    admin_max_accounts: int = 1  # active (not disabled) admins allowed at once

    # Sessions
    session_cookie_name: str = "mv_session"
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"
    session_ttl_hours: int = 24 * 14

    # Tokens
    email_verification_ttl_hours: int = 48
    password_reset_ttl_minutes: int = 60
    invitation_ttl_hours: int = 24 * 7
    unsubscribe_token_ttl_days: int = 90

    # Rate limits ("count/seconds")
    rate_limit_signup_ip: str = "10/3600"
    rate_limit_signup_email: str = "3/3600"
    rate_limit_login_ip: str = "30/900"
    rate_limit_login_email: str = "10/900"
    rate_limit_resend_ip: str = "10/3600"
    rate_limit_resend_email: str = "3/3600"
    rate_limit_reset_ip: str = "10/3600"
    rate_limit_reset_email: str = "3/3600"

    # Sending
    max_recipients_per_message: int = 50
    max_html_bytes: int = 1_000_000
    max_text_bytes: int = 500_000
    default_hourly_recipient_limit: int = 100
    default_daily_recipient_limit: int = 500
    limit_violations_before_suspension: int = 20
    delivery_max_attempts: int = 6
    delivery_backoff_base_seconds: int = 30
    delivery_backoff_max_seconds: int = 3600

    # Account health (see docs/RUNBOOK.md#account-health)
    health_window_days: int = 7
    health_min_volume: int = 100
    health_max_bounce_rate: float = 0.05
    health_max_complaint_rate: float = 0.001
    soft_bounce_threshold: int = 3
    soft_bounce_window_days: int = 30

    # Outbound MTA (Postfix with the OpenDKIM milter)
    smtp_host: str = "localhost"
    smtp_port: int = 25
    smtp_starttls: bool = False
    system_from_email: str = "Mailvender <no-reply@mailvender.localhost>"

    # DNS requirements of the Postfix deployment
    return_path_subdomain: str = "bounce"
    return_path_mx_host: str = "feedback.mailvender.localhost"
    spf_include_domain: str = "_spf.mailvender.localhost"
    dns_nameservers: str = ""
    dns_recheck_interval_minutes: int = 60

    # DKIM key material for OpenDKIM (decrypted copies on a private volume)
    dkim_keys_dir: str = ""

    # AWS S3 asset storage
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket: str = "mailvender-assets"
    aws_s3_public_base_url: str = "http://localhost:9000/mailvender-assets"
    aws_s3_prefix: str = "mailvender/"
    # Optional S3-compatible endpoint instead of AWS (e.g. MinIO); blank = AWS.
    aws_s3_endpoint_url: str = ""
    # The endpoint browsers use when it differs from the API's (custom endpoints only).
    aws_s3_upload_endpoint_url: str = ""
    asset_max_bytes: int = 5 * 1024 * 1024
    asset_upload_url_ttl_seconds: int = 300
    # Dataset content lives in object storage (<prefix>sheets/<user id>/<dataset id>/…); the browser
    # uploads it with a presigned POST and downloads it with a presigned GET.
    dataset_max_bytes: int = 100 * 1024 * 1024
    sync_upload_url_ttl_seconds: int = 900
    sync_content_url_ttl_seconds: int = 900

    # Data lifecycle (see docs/RETENTION.md)
    export_ttl_hours: int = 24
    retention_message_content_days: int = 30
    retention_messages_days: int = 90
    retention_delivery_events_days: int = 180
    retention_deleted_account_days: int = 30
    retention_retired_dkim_days: int = 30
    retention_inbound_mail_days: int = 180
    retention_dmarc_rows_days: int = 365

    @model_validator(mode="after")
    def _check_production(self) -> "Settings":
        """In production, refuse development secrets and mail placeholders (see docs/RUNBOOK.md)."""
        if not self.is_production:
            return self
        problems = []
        if self.secret_key == DEV_SECRET_KEY or len(self.secret_key) < 32:
            problems.append("SECRET_KEY must be at least 32 random characters")
        if self.dkim_encryption_key == DEV_DKIM_ENCRYPTION_KEY:
            problems.append("DKIM_ENCRYPTION_KEY is the development key")
        if self.internal_api_token == DEV_INTERNAL_API_TOKEN:
            problems.append("INTERNAL_API_TOKEN is the development token")
        for name in ("system_from_email", "return_path_mx_host", "spf_include_domain"):
            if "localhost" in getattr(self, name):
                problems.append(f"{name.upper()} is a development placeholder")
        if self.aws_s3_prefix.startswith("dev/"):
            problems.append("AWS_S3_PREFIX is the development folder (dev/); production uses prod/")
        for name in ("admin_proxy_secret", "inbound_mail_secret"):
            value = getattr(self, name)
            if value and (len(value) < 32 or value.startswith("dev-")):
                problems.append(f"{name.upper()} must be at least 32 random characters")
        if self.admin_proxy_secret and self.admin_proxy_secret == self.api_proxy_secret:
            problems.append("ADMIN_PROXY_SECRET must differ from API_PROXY_SECRET")
        if self.webhook_allow_http or self.webhook_allow_private_addresses:
            problems.append("WEBHOOK_ALLOW_HTTP / WEBHOOK_ALLOW_PRIVATE_ADDRESSES are for local development only")
        if problems:
            raise ValueError("invalid production configuration: " + "; ".join(problems))
        return self

    @property
    def feedback_loop_sources(self) -> dict[str, str]:
        sources: dict[str, str] = {}
        for item in filter(None, (part.strip() for part in self.feedback_loop_secrets.split(","))):
            name, _, secret = item.partition(":")
            if name and secret:
                sources[name] = secret
        return sources

    @property
    def admin_cookie_name(self) -> str:
        # __Host- makes the browser insist on Secure, path=/ and no Domain (needs HTTPS).
        return "__Host-mv_admin_session" if self.session_cookie_secure else "mv_admin_session"

    @property
    def known_sending_ip_set(self) -> set[str]:
        return {ip.strip() for ip in self.known_sending_ips.split(",") if ip.strip()}

    @staticmethod
    def _list(value: str) -> list[str]:
        return [item.strip().lower() for item in value.split(",") if item.strip()]

    @property
    def blocklist_ip_zone_list(self) -> list[str]:
        return self._list(self.blocklist_ip_zones)

    @property
    def blocklist_domain_zone_list(self) -> list[str]:
        return self._list(self.blocklist_domain_zones)

    @property
    def link_blocklist_zone_list(self) -> list[str]:
        return self._list(self.link_blocklist_zones)

    @property
    def alert_email_list(self) -> list[str]:
        return self._list(self.alert_emails)

    @property
    def admin_mail_from_list(self) -> list[str]:
        """Addresses an admin can send from, e.g. support@mailvender.com."""
        return [f"{name.strip().lower()}@{self.platform_domain}"
                for name in self.admin_mail_from_mailboxes.split(",") if name.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def platform_domain(self) -> str:
        """Mailvender's own domain (from SYSTEM_FROM_EMAIL): signed with the platform DKIM key, never a customer's."""
        return self.system_from_email.rsplit("@", 1)[-1].strip(">").strip().lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()

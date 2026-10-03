"""Runtime configuration, read from environment variables (see .env.example).

Secrets (database password, signing keys, AWS credentials, the DKIM encryption
key) are only ever supplied through the environment or a secret store; nothing
here has a usable production default.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://mailvender:mailvender@localhost:5434/mailvender"

    # Public URLs. `app_base_url` is the Next.js frontend (links in system
    # emails); `public_api_base_url` is how recipients reach this API
    # (unsubscribe and view-in-browser links), e.g. https://app.example.com/api.
    app_base_url: str = "http://localhost:3000"
    public_api_base_url: str = "http://localhost:3000/api"
    # Behind the Next.js rewrite / a load balancer, take the client IP from X-Forwarded-For.
    trust_forwarded_for: bool = False
    max_request_bytes: int = 25 * 1024 * 1024
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # Secrets. Each must be at least 32 bytes of randomness in production.
    secret_key: str = "dev-only-insecure-secret-key-change-me-please-0000"
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt DKIM private keys.
    dkim_encryption_key: str = "ZGV2LW9ubHktZGtpbS1lbmNyeXB0aW9uLWtleS0wMDA="
    # Shared secret for Postfix → API DSN delivery (`/internal/dsn`).
    internal_api_token: str = "dev-internal-token"
    # Feedback-loop sources and their HMAC secrets: "name:secret,name2:secret2".
    feedback_loop_secrets: str = ""

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
    aws_s3_endpoint_url: str = ""
    # Local development only: the endpoint browsers use when it differs from the
    # API's (e.g. http://localhost:9000 vs http://minio:9000).
    aws_s3_upload_endpoint_url: str = ""
    asset_max_bytes: int = 5 * 1024 * 1024
    asset_upload_url_ttl_seconds: int = 300

    # Data lifecycle (see docs/RETENTION.md)
    export_ttl_hours: int = 24
    retention_message_content_days: int = 30
    retention_messages_days: int = 90
    retention_delivery_events_days: int = 180
    retention_deleted_account_days: int = 30
    retention_retired_dkim_days: int = 30

    @property
    def feedback_loop_sources(self) -> dict[str, str]:
        sources: dict[str, str] = {}
        for item in filter(None, (part.strip() for part in self.feedback_loop_secrets.split(","))):
            name, _, secret = item.partition(":")
            if name and secret:
                sources[name] = secret
        return sources

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()

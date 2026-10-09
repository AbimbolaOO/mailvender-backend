import pytest
from pydantic import ValidationError

from app.config import Settings

PRODUCTION = {
    "environment": "production",
    "secret_key": "x" * 48,
    "dkim_encryption_key": "cHJvZHVjdGlvbi1rZXktcHJvZHVjdGlvbi1rZXktMDA=",
    "internal_api_token": "prod-internal-token",
    "system_from_email": "Mailvender <no-reply@mailvender.example>",
    "return_path_mx_host": "feedback.mailvender.example",
    "spf_include_domain": "_spf.mailvender.example",
}


def test_production_settings_accept_real_values() -> None:
    assert Settings(**PRODUCTION).is_production


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("secret_key", "dev-only-insecure-secret-key-change-me-please-0000", "SECRET_KEY"),
        ("secret_key", "short", "SECRET_KEY"),
        ("dkim_encryption_key", "ZGV2LW9ubHktZGtpbS1lbmNyeXB0aW9uLWtleS0wMDA=", "DKIM_ENCRYPTION_KEY"),
        ("internal_api_token", "dev-internal-token", "INTERNAL_API_TOKEN"),
        ("system_from_email", "Mailvender <no-reply@mailvender.localhost>", "SYSTEM_FROM_EMAIL"),
        ("return_path_mx_host", "feedback.mailvender.localhost", "RETURN_PATH_MX_HOST"),
        ("spf_include_domain", "_spf.mailvender.localhost", "SPF_INCLUDE_DOMAIN"),
        ("aws_s3_prefix", "dev/", "AWS_S3_PREFIX"),
    ],
)
def test_production_settings_reject_development_values(field: str, value: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(**{**PRODUCTION, field: value})


def test_development_settings_keep_their_defaults() -> None:
    assert not Settings(environment="development").is_production

"""Object storage behind an interface; the S3 implementation is the only AWS SDK user."""

from dataclasses import dataclass
from typing import Any, Protocol

from app.config import get_settings


@dataclass(frozen=True)
class PresignedUpload:
    url: str
    fields: dict[str, str]
    expires_in: int


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None


class ObjectStorage(Protocol):
    def presign_upload(self, key: str, content_type: str, max_bytes: int, expires_in: int) -> PresignedUpload: ...
    def head(self, key: str) -> ObjectInfo | None: ...
    def read(self, key: str, max_bytes: int) -> bytes: ...
    def put(self, key: str, body: bytes, content_type: str, *, cache_control: str | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def public_url(self, key: str) -> str: ...


class S3Storage:
    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        settings = get_settings()
        self.bucket = settings.aws_s3_bucket
        self.public_base = settings.aws_s3_public_base_url.rstrip("/")
        self.endpoint = settings.aws_s3_endpoint_url.rstrip("/")
        self.upload_endpoint = settings.aws_s3_upload_endpoint_url.rstrip("/")
        self.client: Any = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
            endpoint_url=settings.aws_s3_endpoint_url or None,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def presign_upload(self, key: str, content_type: str, max_bytes: int, expires_in: int) -> PresignedUpload:
        # A presigned POST pins the key, content type and size range; it can't be reused for other objects.
        post = self.client.generate_presigned_post(
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
                {"key": key},
            ],
            ExpiresIn=expires_in,
        )
        url = post["url"]
        if self.endpoint and self.upload_endpoint:
            # A POST policy's signature doesn't cover the host, so only the URL changes.
            url = url.replace(self.endpoint, self.upload_endpoint, 1)
        return PresignedUpload(url=url, fields=post["fields"], expires_in=expires_in)

    def head(self, key: str) -> ObjectInfo | None:
        from botocore.exceptions import ClientError

        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return None
        return ObjectInfo(size=int(response["ContentLength"]), content_type=response.get("ContentType"))

    def read(self, key: str, max_bytes: int) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{max_bytes - 1}")
        return response["Body"].read()

    def put(self, key: str, body: bytes, content_type: str, *, cache_control: str | None = None) -> None:
        extra = {"CacheControl": cache_control} if cache_control else {}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type, **extra)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def public_url(self, key: str) -> str:
        return f"{self.public_base}/{key}"

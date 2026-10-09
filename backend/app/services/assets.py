"""Email image hosting on S3.

Flow: the browser asks for an upload (`create_upload`) and receives a presigned
POST scoped to one private object key, content type and size range. After the
browser uploads, `finalize` downloads the object, checks that it really is an
allowed image within the size limit, re-publishes it under the account's public
prefix and deletes the private upload. Each user's images live in their own
folder, ``<prefix>images/<user id>/`` (the account id for API-key uploads).
Only finalized paths are publicly readable (bucket policy:
``<prefix>images/*/public/*``).
"""

import io
import re
import uuid
from datetime import timedelta

from PIL import Image, UnidentifiedImageError

from app import models as m
from app.config import get_settings
from app.errors import ApiError, NotFound, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.pagination import Page, PageRequest
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow
from app.services.common import AccountContext, audit
from app.services.storage import ObjectStorage, PresignedUpload

log = get_logger("assets")

# MIME type → Pillow format. SVG is excluded: it can carry script and most email clients block it.
ALLOWED_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
MAX_DIMENSION = 8000

Image.MAX_IMAGE_PIXELS = 40_000_000


def safe_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", base.rsplit(".", 1)[0]).strip("-.") or "image"
    return stem[:80]


class AssetService:
    def __init__(self, uow: UnitOfWork, storage: ObjectStorage):
        self.uow = uow
        self.storage = storage
        self.settings = get_settings()

    def _prefix(self, owner_id: uuid.UUID) -> str:
        return f"{self.settings.aws_s3_prefix}images/{owner_id}"

    def create_upload(self, ctx: AccountContext, filename: str, content_type: str,
                      size: int) -> tuple[m.Asset, PresignedUpload]:
        errors = []
        if content_type not in ALLOWED_TYPES:
            errors.append(field_error("content_type", "Upload a PNG, JPEG, GIF or WebP image.", "unsupported_type"))
        if not 0 < size <= self.settings.asset_max_bytes:
            limit = self.settings.asset_max_bytes // (1024 * 1024)
            errors.append(field_error("size", f"Images can be up to {limit} MB.", "too_large"))
        if not filename.strip():
            errors.append(field_error("filename", "A file name is required.", "required"))
        if errors:
            raise ValidationFailed(errors)
        asset_id = uuid.uuid4()
        owner_id = ctx.user.id if ctx.user else ctx.account_id
        upload_key = f"{self._prefix(owner_id)}/uploads/{asset_id}-{uuid.uuid4().hex}"
        ttl = self.settings.asset_upload_url_ttl_seconds
        asset = self.uow.assets.add(
            m.Asset(
                id=asset_id,
                account_id=ctx.account_id,
                status="pending",
                upload_key=upload_key,
                original_filename=filename.strip()[:255],
                mime_type=content_type,
                declared_size=size,
                created_by=ctx.user.id if ctx.user else None,
                upload_expires_at=utcnow() + timedelta(seconds=ttl),
            )
        )
        # Never larger than what was declared (and never above the global limit).
        upload = self.storage.presign_upload(upload_key, content_type, size, ttl)
        self.uow.commit()
        return asset, upload

    def finalize(self, ctx: AccountContext, asset_id: uuid.UUID) -> m.Asset:
        asset = self.uow.assets.get(asset_id, ctx.account_id)
        if asset is None or asset.status == "deleted":
            raise NotFound("Asset not found.")
        if asset.status == "ready":
            return asset

        def reject(message: str, code: str) -> ApiError:
            self.storage.delete(asset.upload_key)
            asset.status = "deleted"
            asset.deleted_at = utcnow()
            self.uow.commit()
            log_event(log, "asset.rejected", asset_id=str(asset.id), reason=code)
            return ApiError(message, code=code, status_code=422)

        info = self.storage.head(asset.upload_key)
        if info is None:
            raise ApiError("The upload hasn't arrived yet. Upload the file, then finalize.", code="upload_missing",
                           status_code=409)
        if info.size > self.settings.asset_max_bytes or info.size > asset.declared_size:
            raise reject("The uploaded file is larger than allowed.", "too_large")
        body = self.storage.read(asset.upload_key, self.settings.asset_max_bytes + 1)
        if len(body) > self.settings.asset_max_bytes:
            raise reject("The uploaded file is larger than allowed.", "too_large")
        try:
            with Image.open(io.BytesIO(body)) as image:
                image_format, width, height = image.format, image.width, image.height
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError, ValueError):
            raise reject("The uploaded file isn't a valid image.", "invalid_image") from None
        if ALLOWED_TYPES.get(asset.mime_type) != image_format:
            raise reject("The file's contents don't match its type.", "type_mismatch")
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise reject(f"Images can be up to {MAX_DIMENSION}px wide and tall.", "too_large_dimensions")

        public_key = (
            f"{self._prefix(asset.created_by or asset.account_id)}/public/{uuid.uuid4().hex}/"
            f"{safe_filename(asset.original_filename)}.{EXTENSIONS[asset.mime_type]}"
        )
        self.storage.put(public_key, body, asset.mime_type, cache_control="public, max-age=31536000, immutable")
        self.storage.delete(asset.upload_key)
        asset.public_key = public_key
        asset.public_url = self.storage.public_url(public_key)
        asset.byte_size = len(body)
        asset.width, asset.height = width, height
        asset.status = "ready"
        asset.finalized_at = utcnow()
        audit(self.uow, ctx.account_id, ctx.actor, "asset.finalized", target_type="asset", target_id=asset.id)
        self.uow.commit()
        log_event(log, "asset.finalized", asset_id=str(asset.id), bytes=len(body))
        return asset

    def list_page(self, ctx: AccountContext, page: PageRequest) -> Page[m.Asset]:
        return self.uow.assets.list_ready(ctx.account_id, page)

    def get(self, ctx: AccountContext, asset_id: uuid.UUID) -> m.Asset:
        asset = self.uow.assets.get(asset_id, ctx.account_id)
        if asset is None or asset.status != "ready":
            raise NotFound("Asset not found.")
        return asset

    def delete(self, ctx: AccountContext, asset_id: uuid.UUID) -> None:
        asset = self.uow.assets.get(asset_id, ctx.account_id)
        if asset is None or asset.status == "deleted":
            raise NotFound("Asset not found.")
        self._delete_objects(asset)
        audit(self.uow, ctx.account_id, ctx.actor, "asset.deleted", target_type="asset", target_id=asset.id)
        self.uow.commit()

    def _delete_objects(self, asset: m.Asset) -> None:
        # Already-delivered email keeps its copy; future loads of the URL fail.
        if asset.public_key:
            self.storage.delete(asset.public_key)
        self.storage.delete(asset.upload_key)
        asset.status = "deleted"
        asset.deleted_at = utcnow()

    def delete_all_for_account(self, account_id: uuid.UUID) -> int:
        count = 0
        for asset in self.uow.assets.list_all_for_account(account_id):
            if asset.status != "deleted":
                self._delete_objects(asset)
                count += 1
        return count

    def cleanup_abandoned_uploads(self) -> int:
        stale = self.uow.assets.list_stale_pending(utcnow() - timedelta(hours=1), 200)
        for asset in stale:
            self._delete_objects(asset)
        return len(stale)

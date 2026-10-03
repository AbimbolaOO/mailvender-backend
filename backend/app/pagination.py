"""Cursor pagination shared by every list endpoint.

Lists are ordered newest first by (created_at, id). `limit` defaults to 25 and
is capped at MAX_LIMIT; `next_cursor` is null on the last page.
"""

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar

from app.errors import ValidationFailed, field_error

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

T = TypeVar("T")


@dataclass(frozen=True)
class PageRequest:
    limit: int = DEFAULT_LIMIT
    cursor: tuple[datetime, uuid.UUID] | None = None


@dataclass
class Page(Generic[T]):
    items: list[T] = field(default_factory=list)
    next_cursor: str | None = None


def encode_cursor(created_at: datetime, id_: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(id_)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, id_ = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(created_at), uuid.UUID(id_)
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValidationFailed([field_error("cursor", "This cursor is not valid.")]) from error


def page_request(limit: int | None, cursor: str | None) -> PageRequest:
    if limit is not None and not 1 <= limit <= MAX_LIMIT:
        raise ValidationFailed([field_error("limit", f"Use a limit between 1 and {MAX_LIMIT}.")])
    return PageRequest(limit=limit or DEFAULT_LIMIT, cursor=decode_cursor(cursor) if cursor else None)

"""Starter templates: the ready-made emails in the studio's New page gallery.

They're written as TypeScript builders in the studio (``mailvender/lib/editor/templates/``),
checked there (``templates.test.ts``) and exported with ``npm run templates:export`` as one JSON
file per template into ``backend/starter_templates/``. That folder ships in the image; the API
syncs it into ``starter_templates`` when it starts (``python -m app.cli sync-templates``), so a
deploy publishes new and changed templates and removes deleted ones.

The API is built for a growing catalogue and a lazy gallery:

* ``GET /v1/templates`` – summaries only (no documents), in gallery order, keyset-paginated, filtered
  by category and search words on the server.
* ``GET /v1/templates/categories`` – categories with counts, and the catalogue ``version``.
* ``GET /v1/templates/{id}`` – one template with its document, and its own ``version``.

Every response carries an ETag (``304`` when unchanged). The studio caches documents by ``version``
and list pages by catalogue ``version``, so nothing is downloaded twice.
"""

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import models as m
from app.errors import NotFound, ValidationFailed, field_error
from app.logging import get_logger, log_event
from app.repositories.interfaces import UnitOfWork
from app.security import utcnow

log = get_logger("templates")

FOLDER = Path(__file__).resolve().parents[2] / "starter_templates"
DOC_VERSION = 3  # EmailDoc.version in the studio (lib/editor/types.ts)
MAX_DOC_BYTES = 1_000_000
STATE_KEY = "starter_templates"
TEMPLATE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
RESERVED_IDS = {"categories"}  # GET /v1/templates/categories
MAX_SEARCH_WORDS = 8


@dataclass(frozen=True)
class TemplateFile:
    id: str
    name: str
    category: str
    description: str
    tags: list[str]
    order: int
    doc: dict[str, Any]

    @property
    def content_hash(self) -> str:
        payload = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()


def version_of(template: m.StarterTemplate) -> str:
    """Changes whenever the template does (clients cache its document by it)."""
    return template.content_hash[:16]


def _encode_cursor(template: m.StarterTemplate) -> str:
    raw = json.dumps([template.sort_order, template.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        order, template_id = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(order, int) or not isinstance(template_id, str):
            raise ValueError
        return order, template_id
    except (ValueError, TypeError) as error:
        raise ValidationFailed([field_error("cursor", "This cursor is not valid.")]) from error


def search_words(query: str | None) -> list[str]:
    return [word for word in (query or "").lower().split() if word][:MAX_SEARCH_WORDS]


@dataclass(frozen=True)
class TemplatePage:
    items: list[m.StarterTemplate]
    next_cursor: str | None


@dataclass(frozen=True)
class SyncResult:
    added: list[str]
    updated: list[str]
    removed: list[str]
    unchanged: int


def _text(data: dict[str, Any], key: str, limit: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{key!r} must be text of 1–{limit} characters")
    return value


def parse_template(name: str, raw: bytes) -> TemplateFile:
    """One exported template, validated. Raises ValueError naming what's wrong."""
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ValueError(f"{name}: not valid JSON ({error})") from None
    try:
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        template_id = _text(data, "id", 80)
        if not TEMPLATE_ID.fullmatch(template_id):
            raise ValueError("'id' must be lowercase letters, digits and dashes")
        if template_id in RESERVED_IDS:
            raise ValueError(f"'{template_id}' is reserved")
        if name != f"{template_id}.json":
            raise ValueError(f"the file must be named {template_id}.json")
        tags = data.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) and len(tag) <= 60 for tag in tags):
            raise ValueError("'tags' must be a list of short strings")
        order = data.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            raise ValueError("'order' must be a whole number")
        doc = data.get("doc")
        if not isinstance(doc, dict) or doc.get("version") != DOC_VERSION:
            raise ValueError(f"'doc' must be an email document of version {DOC_VERSION}")
        if not isinstance(doc.get("settings"), dict) or not isinstance(doc.get("children"), list):
            raise ValueError("'doc' needs 'settings' and 'children'")
        if len(json.dumps(doc)) > MAX_DOC_BYTES:
            raise ValueError(f"'doc' is larger than {MAX_DOC_BYTES // 1000} KB")
        return TemplateFile(
            id=template_id,
            name=_text(data, "name", 120),
            category=_text(data, "category", 40),
            description=_text(data, "description", 2000),
            tags=tags,
            order=order,
            doc=doc,
        )
    except ValueError as error:
        raise ValueError(f"{name}: {error}") from None


def read_folder(folder: Path = FOLDER) -> list[TemplateFile]:
    """Every ``<id>.json`` in the folder (all valid, or ValueError)."""
    if not folder.is_dir():
        raise ValueError(f"{folder} isn't a folder")
    return [parse_template(path.name, path.read_bytes()) for path in sorted(folder.glob("*.json"))]


class TemplateService:
    def __init__(self, uow: UnitOfWork):
        self.uow = uow

    def sync(self, files: list[TemplateFile]) -> SyncResult:
        """Make the table match the exported files: add new, update changed, remove missing."""
        if len({file.id for file in files}) != len(files):
            raise ValueError("two templates have the same id")
        existing = {row.id: row for row in self.uow.starter_templates.list_all()}
        now = utcnow()
        added: list[str] = []
        updated: list[str] = []
        unchanged = 0
        for file in files:
            row = existing.pop(file.id, None)
            if row is not None and row.content_hash == file.content_hash:
                unchanged += 1
                continue
            if row is None:
                row = m.StarterTemplate(id=file.id, created_at=now)
                added.append(file.id)
            else:
                updated.append(file.id)
            row.name, row.category, row.description = file.name, file.category, file.description
            row.tags, row.doc, row.sort_order = file.tags, file.doc, file.order
            row.content_hash, row.updated_at = file.content_hash, now
            if file.id in added:
                self.uow.starter_templates.add(row)
        removed = sorted(existing)
        for row in existing.values():
            self.uow.starter_templates.delete(row)
        # The catalogue's ETag changes whenever any template does.
        catalogue = sorted(files, key=lambda file: (file.order, file.id))
        etag = hashlib.sha256("\n".join(f"{file.id}:{file.content_hash}" for file in catalogue).encode()).hexdigest()
        self.uow.system_state.put(STATE_KEY, {"etag": etag[:32], "count": len(files)}, now)
        self.uow.commit()
        if added or updated or removed:
            log_event(log, "templates.synced", added=len(added), updated=len(updated),
                      removed=len(removed))
        return SyncResult(added=added, updated=updated, removed=removed, unchanged=unchanged)

    def catalog_version(self) -> str:
        """Changes whenever any template is added, changed, reordered or removed."""
        state = self.uow.system_state.get(STATE_KEY) or {}
        return str(state.get("etag", "empty"))

    def page(self, *, limit: int, cursor: str | None, category: str | None, query: str | None) -> TemplatePage:
        """Summaries in gallery order. Fetches one extra row to know whether there's a next page."""
        rows = self.uow.starter_templates.page(limit=limit + 1, after=_decode_cursor(cursor) if cursor else None,
                                               category=category or None, words=search_words(query))
        more = len(rows) > limit
        items = rows[:limit]
        return TemplatePage(items=items, next_cursor=_encode_cursor(items[-1]) if more else None)

    def categories(self) -> list[tuple[str, int]]:
        return self.uow.starter_templates.categories()

    def get(self, template_id: str) -> m.StarterTemplate:
        template = self.uow.starter_templates.get(template_id)
        if template is None:
            raise NotFound("Template not found.")
        return template

    def list_all(self) -> list[m.StarterTemplate]:
        return self.uow.starter_templates.list_all()

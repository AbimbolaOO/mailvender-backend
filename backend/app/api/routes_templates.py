"""Starter templates for the studio's New page gallery (services/templates.py).

Built for a lazy gallery: the list has summaries only (paginated, filtered on the server), and each
document is fetched on its own when its preview comes into view. Every response has an ETag, so a
client that sends it back (`If-None-Match`; browsers do this themselves) gets `304 Not Modified`.
"""

import hashlib
from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Response

from app.api import schemas as s
from app.api.deps import CurrentUser, Uow
from app.errors import ERROR_RESPONSES
from app.pagination import MAX_LIMIT
from app.services.templates import TemplateService, version_of

router = APIRouter(prefix="/v1/templates", tags=["templates"], responses=ERROR_RESPONSES)

IfNoneMatch = Annotated[str | None, Header(max_length=200)]
NOT_MODIFIED = {304: {"description": "Not modified (the `If-None-Match` ETag is current)."}}


def _cached(etag: str, if_none_match: str | None, response: Response) -> Response | None:
    """Sets the caching headers; returns a 304 response when the client's copy is current."""
    tag = f'"{etag}"'
    # Revalidated on every use, so a deploy's changes show up at once.
    headers = {"ETag": tag, "Cache-Control": "private, no-cache"}
    if if_none_match and tag in [value.strip() for value in if_none_match.split(",")]:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return None


def _summary(template) -> s.StarterTemplateSummaryOut:  # type: ignore[no-untyped-def]
    return s.StarterTemplateSummaryOut(id=template.id, name=template.name, category=template.category,
                                       description=template.description, tags=template.tags,
                                       version=version_of(template))


@router.get("", response_model=s.StarterTemplatePageOut, responses=NOT_MODIFIED,
            summary="Template summaries, in gallery order (no documents)")
def list_templates(
    _: CurrentUser,
    uow: Uow,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 24,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    category: Annotated[str | None, Query(max_length=40)] = None,
    q: Annotated[str | None, Query(max_length=200, description="Words that must all appear (name, "
                                                                  "category, description or tags).")] = None,
    if_none_match: IfNoneMatch = None,
) -> s.StarterTemplatePageOut | Response:
    service = TemplateService(uow)
    version = service.catalog_version()
    etag = hashlib.sha256(f"{version}|{limit}|{cursor}|{category}|{q}".encode()).hexdigest()[:32]
    if (not_modified := _cached(etag, if_none_match, response)) is not None:
        return not_modified
    page = service.page(limit=limit, cursor=cursor, category=category, query=q)
    return s.StarterTemplatePageOut(items=[_summary(t) for t in page.items], next_cursor=page.next_cursor,
                                    catalog_version=version)


@router.get("/categories", response_model=s.TemplateCategoriesOut, responses=NOT_MODIFIED,
            summary="Categories with counts, and the catalogue version")
def template_categories(_: CurrentUser, uow: Uow, response: Response,
                        if_none_match: IfNoneMatch = None) -> s.TemplateCategoriesOut | Response:
    service = TemplateService(uow)
    version = service.catalog_version()
    if (not_modified := _cached(version, if_none_match, response)) is not None:
        return not_modified
    categories = service.categories()
    return s.TemplateCategoriesOut(catalog_version=version, total=sum(count for _, count in categories),
                                   categories=[s.TemplateCategoryOut(name=name, count=count)
                                               for name, count in categories])


@router.get("/{template_id}", response_model=s.StarterTemplateOut, responses=NOT_MODIFIED,
            summary="One template with its document")
def get_template(template_id: Annotated[str, Path(max_length=80)], _: CurrentUser, uow: Uow, response: Response,
                 if_none_match: IfNoneMatch = None) -> s.StarterTemplateOut | Response:
    template = TemplateService(uow).get(template_id)
    if (not_modified := _cached(version_of(template), if_none_match, response)) is not None:
        return not_modified
    return s.StarterTemplateOut(**_summary(template).model_dump(), doc=template.doc)

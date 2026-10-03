"""Unauthenticated endpoints: recipient links, MTA/feedback-loop callbacks, health."""

import html
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.api import schemas as s
from app.api.deps import Uow
from app.config import get_settings
from app.db import database_is_ready
from app.errors import ERROR_RESPONSES, Unauthenticated
from app.security import constant_time_equals
from app.services.feedback import FeedbackService

router = APIRouter(responses=ERROR_RESPONSES)

MAX_FEEDBACK_BYTES = 2 * 1024 * 1024


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#18181b}}
button{{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:6px;background:#18181b;color:#fff;cursor:pointer}}
@media (prefers-color-scheme:dark){{body{{background:#18181b;color:#f4f4f5}}button{{background:#f4f4f5;color:#18181b}}}}
</style></head><body>{body}</body></html>""",
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


INVALID_LINK = "<h1>This link has expired</h1><p>Use the unsubscribe link in a more recent email.</p>"


# ---- Health ----


@router.get("/health/live", tags=["health"], summary="The process is up")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", tags=["health"], summary="Ready to serve (database reachable)",
            responses={503: {"description": "Not ready"}})
def ready() -> JSONResponse:
    if not database_is_ready():
        return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})


# ---- Recipient links ----


@router.get("/u/{token}", response_class=HTMLResponse, tags=["recipients"], summary="Unsubscribe page")
def unsubscribe_page(token: str) -> HTMLResponse:
    # A GET never unsubscribes on its own: link scanners prefetch URLs.
    return _page(
        "Unsubscribe",
        "<h1>Unsubscribe</h1><p>Stop receiving these emails?</p>"
        f'<form method="post" action="{html.escape(token)}"><input type="hidden" name="List-Unsubscribe" '
        'value="One-Click"><button type="submit">Unsubscribe</button></form>',
    )


@router.post("/u/{token}", response_class=HTMLResponse, tags=["recipients"],
             summary="One-click unsubscribe (RFC 8058) – no login, idempotent")
async def unsubscribe(token: str, request: Request, uow: Uow) -> HTMLResponse:
    form = (await request.body()).decode(errors="replace")
    source = "one_click" if "List-Unsubscribe=One-Click" in form else "link"
    recipient = FeedbackService(uow).unsubscribe(token, source)
    if recipient is None:
        return _page("Link expired", INVALID_LINK, 400)
    return _page(
        "Unsubscribed",
        f"<h1>You're unsubscribed</h1><p>{html.escape(recipient.email)} won't get these emails any more.</p>",
    )


@router.get("/v/{token}", response_class=HTMLResponse, tags=["recipients"], summary="View the email in a browser")
def view_in_browser(token: str, uow: Uow) -> HTMLResponse:
    content = FeedbackService(uow).view_in_browser(token)
    if content is None:
        return _page("Not available", "<h1>This email is no longer available</h1>", 404)
    return HTMLResponse(
        content,
        headers={
            # The email's own HTML: no scripts, no framing.
            "Content-Security-Policy": "script-src 'none'; frame-ancestors 'none'; object-src 'none'",
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


# ---- Postfix and feedback loops ----


def _internal(token: str | None) -> None:
    if not token or not constant_time_equals(token, get_settings().internal_api_token):
        raise Unauthenticated("Invalid internal token.", code="invalid_internal_token")


@router.post("/internal/dsn", response_model=s.FeedbackResultOut, tags=["internal"],
             summary="Postfix delivers bounce messages (DSNs) here",
             description="Body: the raw RFC 3464 message. `recipient` is the envelope recipient of the DSN "
                         "(the VERP return-path address). Authenticated with X-Internal-Token.")
async def receive_dsn(
    request: Request,
    uow: Uow,
    x_internal_token: Annotated[str | None, Header()] = None,
    recipient: Annotated[str | None, Query(max_length=320)] = None,
) -> s.FeedbackResultOut:
    _internal(x_internal_token)
    raw = await request.body()
    result = FeedbackService(uow).process_dsn(raw[:MAX_FEEDBACK_BYTES], recipient)
    return s.FeedbackResultOut.model_validate(result)


@router.post("/internal/feedback-loop/{source}", response_model=s.FeedbackResultOut, tags=["internal"],
             summary="Complaint reports (ARF) from a configured feedback-loop source",
             description="Authenticated per source: X-Mailvender-Signature: sha256=<HMAC-SHA256 of the body with "
                         "the source's secret> (FEEDBACK_LOOP_SECRETS).")
async def receive_complaint(
    source: str,
    request: Request,
    uow: Uow,
    x_mailvender_signature: Annotated[str | None, Header()] = None,
) -> s.FeedbackResultOut:
    raw = (await request.body())[:MAX_FEEDBACK_BYTES]
    service = FeedbackService(uow)
    service.verify_feedback_source(source, raw, x_mailvender_signature)
    return s.FeedbackResultOut.model_validate(service.process_complaint(source, raw))


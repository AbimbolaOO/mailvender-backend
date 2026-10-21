"""Webhooks: an account's endpoints that receive signed event notifications (owners).

Events, signing and retries are described in services/webhooks.py and, for customers, in
docs/WEBHOOKS.md.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api import schemas as s
from app.api.deps import OwnerCtx, Uow
from app.errors import ERROR_RESPONSES
from app.pagination import MAX_LIMIT, page_request
from app.services.webhooks import EVENTS, WebhookService

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"], responses=ERROR_RESPONSES)

Limit = Annotated[int | None, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=1000)]

SIGNING = ("Each request carries `Mailvender-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of \"<t>.<raw body>\" "
           "with the endpoint secret>`, `Mailvender-Event` and `Mailvender-Event-Id` (the same on retries – use it "
           "to ignore duplicates). Answer 2xx within 10 s; anything else is retried with backoff for about 1.5 days. "
           "An endpoint that fails for 3 days is turned off and the owners are emailed.")


@router.get("/events", response_model=list[s.WebhookEventTypeOut], summary="The events an endpoint can receive")
def event_types(_: OwnerCtx) -> list[s.WebhookEventTypeOut]:
    return [s.WebhookEventTypeOut(type=name, description=text) for name, text in EVENTS.items()]


@router.get("", response_model=list[s.WebhookEndpointOut], summary="The account's webhook endpoints")
def list_webhooks(ctx: OwnerCtx, uow: Uow) -> list[s.WebhookEndpointOut]:
    return [s.WebhookEndpointOut.model_validate(e) for e in WebhookService(uow).list_endpoints(ctx)]


@router.post("", status_code=201, response_model=s.WebhookSecretOut, summary="Add an endpoint (returns its secret once)",
             description=SIGNING)
def create_webhook(body: s.WebhookIn, ctx: OwnerCtx, uow: Uow) -> s.WebhookSecretOut:
    endpoint, secret = WebhookService(uow).create(ctx, body.url, body.events, body.description)
    return s.WebhookSecretOut(endpoint=s.WebhookEndpointOut.model_validate(endpoint), secret=secret)


@router.get("/{webhook_id}", response_model=s.WebhookEndpointOut)
def get_webhook(webhook_id: uuid.UUID, ctx: OwnerCtx, uow: Uow) -> s.WebhookEndpointOut:
    return s.WebhookEndpointOut.model_validate(WebhookService(uow).get(ctx, webhook_id))


@router.patch("/{webhook_id}", response_model=s.WebhookEndpointOut,
              summary="Change the URL, events or description, or turn it on/off")
def update_webhook(webhook_id: uuid.UUID, body: s.WebhookUpdateIn, ctx: OwnerCtx, uow: Uow) -> s.WebhookEndpointOut:
    return s.WebhookEndpointOut.model_validate(WebhookService(uow).update(
        ctx, webhook_id, url=body.url, events=body.events, description=body.description, enabled=body.enabled))


@router.delete("/{webhook_id}", status_code=204, summary="Delete an endpoint (and its delivery history)")
def delete_webhook(webhook_id: uuid.UUID, ctx: OwnerCtx, uow: Uow) -> Response:
    WebhookService(uow).delete(ctx, webhook_id)
    return Response(status_code=204)


@router.post("/{webhook_id}/rotate-secret", response_model=s.WebhookSecretOut,
             summary="Make a new secret (the old one stops working at once)")
def rotate_secret(webhook_id: uuid.UUID, ctx: OwnerCtx, uow: Uow) -> s.WebhookSecretOut:
    endpoint, secret = WebhookService(uow).rotate_secret(ctx, webhook_id)
    return s.WebhookSecretOut(endpoint=s.WebhookEndpointOut.model_validate(endpoint), secret=secret)


@router.post("/{webhook_id}/test", status_code=202, response_model=s.WebhookDeliveryOut,
             summary="Send a `webhook.test` event now (tried once; works while the endpoint is off)")
def send_test(webhook_id: uuid.UUID, ctx: OwnerCtx, uow: Uow) -> s.WebhookDeliveryOut:
    return s.WebhookDeliveryOut.model_validate(WebhookService(uow).send_test(ctx, webhook_id))


@router.get("/{webhook_id}/deliveries", response_model=s.PageOut[s.WebhookDeliveryOut],
            summary="Recent deliveries to this endpoint, newest first (kept 30 days)")
def deliveries(webhook_id: uuid.UUID, ctx: OwnerCtx, uow: Uow, limit: Limit = None,
               cursor: Cursor = None) -> s.PageOut[s.WebhookDeliveryOut]:
    page = WebhookService(uow).deliveries(ctx, webhook_id, page_request(limit, cursor))
    return s.PageOut[s.WebhookDeliveryOut](items=[s.WebhookDeliveryOut.model_validate(d) for d in page.items],
                                           next_cursor=page.next_cursor)


@router.post("/{webhook_id}/deliveries/{delivery_id}/retry", status_code=202, response_model=s.WebhookDeliveryOut,
             summary="Send a delivered or failed event again")
def retry_delivery(webhook_id: uuid.UUID, delivery_id: uuid.UUID, ctx: OwnerCtx, uow: Uow) -> s.WebhookDeliveryOut:
    return s.WebhookDeliveryOut.model_validate(WebhookService(uow).retry(ctx, webhook_id, delivery_id))

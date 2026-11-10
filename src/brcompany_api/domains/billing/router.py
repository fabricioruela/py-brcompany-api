"""FastAPI router for Plans, PIX Checkout, Real-Time SSE, Asaas Webhooks, and Wallet Ledger."""

import asyncio
import json
from typing import Annotated, Any, Dict, List, Optional
from uuid import UUID
import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse

from brcompany_api.config import Settings, get_settings
from brcompany_api.db import get_db_conn
from brcompany_api.domains.auth.dependencies import get_current_user
from brcompany_api.domains.billing import service
from brcompany_api.domains.billing.schemas import (
    PLANS_CATALOG,
    CheckoutPixRequest,
    OrderResponse,
    PlanItem,
    WalletOverviewResponse,
)

router = APIRouter(tags=["Billing, PIX Checkout & Wallet"])


@router.get(
    "/checkout/plans",
    response_model=List[PlanItem],
    summary="List Available Credit Packages & Smart Subscription",
)
async def list_plans():
    return [PlanItem(**p) for p in PLANS_CATALOG.values()]


@router.post(
    "/checkout/pix",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Instant PIX Charge (Starter, Pro, Scale, or Smart Subscription)",
)
async def create_pix_checkout(
    body: CheckoutPixRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    order_row = await service.create_pix_order(
        conn,
        user=current_user,
        plan_code=body.plan_code,
        tax_id=body.tax_id,
    )
    return OrderResponse.model_validate(order_row)


@router.get(
    "/checkout/sse/{order_id}",
    summary="Real-Time Server-Sent Events (SSE) Stream for PIX Payment Confirmation",
)
async def checkout_order_sse(
    order_id: UUID,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    order = await service.get_order_by_id(conn, order_id)
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pedido não encontrado.",
        )

    async def event_generator():
        if order["status"] == "paid":
            payload = {
                "event": "payment_confirmed",
                "order_id": str(order_id),
                "status": "paid",
                "credits_added": int(order["credits_amount"]),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            return

        q = service.subscribe_order_sse(str(order_id))
        try:
            # Send initial connected heartbeat
            yield f"data: {json.dumps({'event': 'waiting_payment', 'order_id': str(order_id), 'status': order['status']})}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("status") == "paid":
                        break
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            service.unsubscribe_order_sse(str(order_id), q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/checkout/simulate-confirm/{order_id}",
    response_model=OrderResponse,
    summary="Simulate Instant PIX Confirmation (Sandbox / Dev Testing)",
)
async def simulate_pix_confirmation(
    order_id: UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    if settings.app_env == "production" and settings.app_asaas_api_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Simulação disponível apenas em modo sandbox/desenvolvimento.",
        )
    order = await service.get_order_by_id(conn, order_id)
    if not order or order["user_id"] != current_user["id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado.")

    confirmed = await service.confirm_order_payment(
        conn,
        order_id=order_id,
        event_id=f"sim_{order_id}",
        raw_payload={"simulated": True, "order_id": str(order_id)},
    )
    return OrderResponse.model_validate(confirmed)


@router.post(
    "/webhooks/asaas",
    summary="Idempotent Asaas Payment Webhook Handler",
)
async def asaas_webhook(
    payload: Dict[str, Any],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    asaas_access_token: Annotated[Optional[str], Header(alias="asaas-access-token")] = None,
):
    if settings.app_asaas_webhook_token and asaas_access_token != settings.app_asaas_webhook_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Asaas webhook token.",
        )

    event_type = payload.get("event", "")
    event_id = payload.get("id") or f"{event_type}_{payload.get('payment', {}).get('id', '')}"
    payment_obj = payload.get("payment") or {}

    if event_type not in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED"):
        return {"status": "ignored", "event": event_type}

    ext_ref = payment_obj.get("externalReference")
    gw_id = payment_obj.get("id")

    order_id: Optional[UUID] = None
    if ext_ref:
        try:
            order_id = UUID(str(ext_ref))
        except Exception:
            pass

    if not order_id and gw_id:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id FROM app_saas.orders WHERE gateway_order_id = %(gw_id)s LIMIT 1;",
                {"gw_id": gw_id},
            )
            row = await cur.fetchone()
            if row:
                order_id = row["id"]

    if not order_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order reference not found for webhook.",
        )

    updated = await service.confirm_order_payment(
        conn,
        order_id=order_id,
        event_id=event_id,
        raw_payload=payload,
    )
    return {"status": "processed", "order_id": str(updated["id"]), "order_status": updated["status"]}


@router.get(
    "/wallet",
    response_model=WalletOverviewResponse,
    summary="Get User FIFO Wallet Balance, Credit Lots, Transactions & Orders",
)
async def get_wallet(
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    data = await service.get_wallet_overview(conn, user_id=current_user["id"])
    return WalletOverviewResponse.model_validate(data)

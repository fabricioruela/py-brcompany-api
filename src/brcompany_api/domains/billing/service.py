"""Checkout, Asaas Payment Gateway integration, Real-Time SSE notification hub, and FIFO Wallet service."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException, status

from brcompany_api.config import get_settings
from brcompany_api.domains.auth.service import grant_credit_lot, sync_wallet_balance
from brcompany_api.domains.billing.schemas import PLANS_CATALOG

logger = logging.getLogger(__name__)

# In-memory SSE subscriber queues keyed by order_id string
_sse_Waiters: Dict[str, List[asyncio.Queue]] = {}


def subscribe_order_sse(order_id: str) -> asyncio.Queue:
    """Register an asyncio Queue listening for updates on order_id."""
    q: asyncio.Queue = asyncio.Queue()
    _sse_Waiters.setdefault(order_id, []).append(q)
    return q


def unsubscribe_order_sse(order_id: str, q: asyncio.Queue) -> None:
    """Remove an asyncio Queue listener for order_id."""
    waiters = _sse_Waiters.get(order_id)
    if waiters and q in waiters:
        waiters.remove(q)
        if not waiters:
            _sse_Waiters.pop(order_id, None)


def broadcast_order_sse(order_id: str, payload: Dict[str, Any]) -> None:
    """Push a payment confirmation event to all connected SSE listeners for order_id."""
    waiters = _sse_Waiters.get(order_id, [])
    for q in list(waiters):
        try:
            q.put_nowait(payload)
        except Exception:
            pass


def _generate_sandbox_qr_svg_base64(order_id: UUID, amount_cents: int) -> str:
    """Generate a clean SVG QR Code representation encoded in base64 for sandbox/local mode."""
    price_str = f"R$ {amount_cents / 100:.2f}"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 220 220" width="220" height="220">
      <rect width="220" height="220" rx="12" fill="#ffffff"/>
      <rect x="16" y="16" width="54" height="54" fill="none" stroke="#0f172a" stroke-width="8"/>
      <rect x="30" y="30" width="26" height="26" fill="#0f172a"/>
      <rect x="150" y="16" width="54" height="54" fill="none" stroke="#0f172a" stroke-width="8"/>
      <rect x="164" y="30" width="26" height="26" fill="#0f172a"/>
      <rect x="16" y="150" width="54" height="54" fill="none" stroke="#0f172a" stroke-width="8"/>
      <rect x="30" y="164" width="26" height="26" fill="#0f172a"/>
      <rect x="84" y="24" width="12" height="12" fill="#0f172a"/>
      <rect x="108" y="36" width="24" height="12" fill="#0f172a"/>
      <rect x="84" y="60" width="36" height="12" fill="#0f172a"/>
      <rect x="24" y="84" width="36" height="12" fill="#0f172a"/>
      <rect x="76" y="84" width="68" height="52" rx="6" fill="#06b6d4"/>
      <text x="110" y="108" font-family="monospace" font-size="11" font-weight="bold" fill="#ffffff" text-anchor="middle">PIX PANORAMA</text>
      <text x="110" y="124" font-family="monospace" font-size="11" font-weight="bold" fill="#ffffff" text-anchor="middle">{price_str}</text>
      <rect x="156" y="96" width="36" height="12" fill="#0f172a"/>
      <rect x="84" y="150" width="24" height="24" fill="#0f172a"/>
      <rect x="120" y="162" width="36" height="12" fill="#0f172a"/>
      <rect x="168" y="150" width="24" height="36" fill="#0f172a"/>
      <rect x="96" y="186" width="60" height="12" fill="#0f172a"/>
    </svg>"""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


async def _create_asaas_pix_charge(
    user: dict,
    order_id: UUID,
    plan: Dict[str, Any],
    tax_id: str,
) -> Dict[str, Any]:
    """Call Asaas API v3 to create customer + dynamic PIX charge, or fall back to sandbox mode."""
    settings = get_settings()
    if not settings.app_asaas_api_key:
        copy_paste = (
            f"00020126580014br.gov.bcb.pix0136{order_id}"
            f"5204000053039865406{plan['amount_cents'] / 100:.2f}5802BR5910PANORAMAPJ6009SAO_PAULO62070503***6304ABCD"
        )
        return {
            "gateway_order_id": f"asaas_sandbox_{order_id}",
            "pix_qr_code": _generate_sandbox_qr_svg_base64(order_id, plan["amount_cents"]),
            "pix_copy_paste": copy_paste,
        }

    headers = {"access_token": settings.app_asaas_api_key, "Content-Type": "application/json"}
    base_url = settings.app_asaas_api_url.rstrip("/")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Create or find customer
        cust_resp = await client.post(
            f"{base_url}/customers",
            headers=headers,
            json={
                "name": user["full_name"],
                "email": user["email"],
                "cpfCnpj": tax_id,
                "externalReference": str(user["id"]),
            },
        )
        cust_resp.raise_for_status()
        customer_id = cust_resp.json()["id"]

        # 2. Create PIX payment
        due_date = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        pay_resp = await client.post(
            f"{base_url}/payments",
            headers=headers,
            json={
                "customer": customer_id,
                "billingType": "PIX",
                "value": round(plan["amount_cents"] / 100.0, 2),
                "dueDate": due_date,
                "description": f"PanoramaPJ - {plan['name']} ({plan['credits']} créditos)",
                "externalReference": str(order_id),
            },
        )
        pay_resp.raise_for_status()
        payment_id = pay_resp.json()["id"]

        # 3. Fetch PIX QR Code & payload
        qr_resp = await client.get(
            f"{base_url}/payments/{payment_id}/pixQrCode",
            headers=headers,
        )
        qr_resp.raise_for_status()
        qr_data = qr_resp.json()
        encoded_image = qr_data.get("encodedImage", "")
        if encoded_image and not encoded_image.startswith("data:"):
            encoded_image = f"data:image/png;base64,{encoded_image}"

        return {
            "gateway_order_id": payment_id,
            "pix_qr_code": encoded_image,
            "pix_copy_paste": qr_data.get("payload", ""),
        }


async def create_pix_order(
    conn: psycopg.AsyncConnection,
    user: dict,
    plan_code: str,
    tax_id: Optional[str] = None,
) -> dict:
    """Create a PIX order (for Starter, Pro, Scale, or Assinatura Smart) and return QR Code + Copy/Paste."""
    plan = PLANS_CATALOG.get(plan_code)
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plano inválido: {plan_code}",
        )

    effective_tax_id = "".join(ch for ch in (tax_id or user.get("tax_id") or "") if ch.isdigit())
    if effective_tax_id and len(effective_tax_id) not in (11, 14):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CPF (11 dígitos) ou CNPJ (14 dígitos) inválido.",
        )

    if effective_tax_id and effective_tax_id != user.get("tax_id"):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE app_saas.users SET tax_id = %(tax_id)s, updated_at = clock_timestamp() WHERE id = %(id)s;",
                {"tax_id": effective_tax_id, "id": user["id"]},
            )

    order_id = uuid4()
    pix_info = await _create_asaas_pix_charge(
        user=user,
        order_id=order_id,
        plan=plan,
        tax_id=effective_tax_id or "00000000000",
    )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.orders (
                id, user_id, plan_code, gateway_order_id, payment_method, status,
                amount_cents, credits_amount, pix_qr_code, pix_copy_paste, expires_at
            ) VALUES (
                %(id)s, %(user_id)s, %(plan_code)s, %(gateway_order_id)s, 'pix', 'pending',
                %(amount_cents)s, %(credits_amount)s, %(pix_qr_code)s, %(pix_copy_paste)s, %(expires_at)s
            )
            RETURNING *;
            """,
            {
                "id": order_id,
                "user_id": user["id"],
                "plan_code": plan_code,
                "gateway_order_id": pix_info["gateway_order_id"],
                "amount_cents": plan["amount_cents"],
                "credits_amount": plan["credits"],
                "pix_qr_code": pix_info["pix_qr_code"],
                "pix_copy_paste": pix_info["pix_copy_paste"],
                "expires_at": expires_at,
            },
        )
        order_row = await cur.fetchone()
    await conn.commit()
    return order_row


async def confirm_order_payment(
    conn: psycopg.AsyncConnection,
    order_id: UUID,
    event_id: Optional[str] = None,
    raw_payload: Optional[Dict[str, Any]] = None,
) -> dict:
    """Idempotently confirm an order payment, credit FIFO lot, activate subscription if Smart, and notify SSE."""
    settings = get_settings()
    async with conn.cursor(row_factory=dict_row) as cur:
        if event_id:
            await cur.execute(
                "SELECT id FROM app_saas.webhook_events WHERE event_id = %(event_id)s LIMIT 1;",
                {"event_id": event_id},
            )
            if await cur.fetchone():
                # Already processed idempotently
                await cur.execute("SELECT * FROM app_saas.orders WHERE id = %(id)s;", {"id": order_id})
                return await cur.fetchone()

            await cur.execute(
                """
                INSERT INTO app_saas.webhook_events (event_id, gateway, event_type, payload)
                VALUES (%(event_id)s, 'asaas', 'PAYMENT_CONFIRMED', %(payload)s::jsonb);
                """,
                {"event_id": event_id, "payload": json.dumps(raw_payload or {})},
            )

        await cur.execute(
            "SELECT * FROM app_saas.orders WHERE id = %(id)s FOR UPDATE;",
            {"id": order_id},
        )
        order = await cur.fetchone()
        if not order:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Pedido {order_id} não encontrado.",
            )

        if order["status"] == "paid":
            return order

        plan = PLANS_CATALOG.get(order["plan_code"], {})
        is_subscription = plan.get("kind") == "subscription" or order["plan_code"] == "smart_monthly"
        expires_in_days = 90 if is_subscription else None
        source_type = "subscription" if is_subscription else "avulso"

        nfse_status = "scheduled" if settings.app_asaas_auto_nfse else "manual"
        nfse_url = f"https://sandbox.asaas.com/nfse/{order['gateway_order_id']}" if settings.app_asaas_auto_nfse else None

        await cur.execute(
            """
            UPDATE app_saas.orders
            SET status = 'paid',
                paid_at = clock_timestamp(),
                nfse_status = %(nfse_status)s,
                nfse_url = COALESCE(%(nfse_url)s, nfse_url)
            WHERE id = %(id)s
            RETURNING *;
            """,
            {"id": order_id, "nfse_status": nfse_status, "nfse_url": nfse_url},
        )
        updated_order = await cur.fetchone()

        if is_subscription:
            period_end = datetime.now(timezone.utc) + timedelta(days=30)
            await cur.execute(
                """
                INSERT INTO app_saas.subscriptions (
                    id, user_id, plan_code, gateway_subscription_id, payment_method, status, current_period_end
                ) VALUES (
                    %(id)s, %(user_id)s, %(plan_code)s, %(gw_sub)s, %(method)s, 'active', %(period_end)s
                );
                """,
                {
                    "id": uuid4(),
                    "user_id": order["user_id"],
                    "plan_code": order["plan_code"],
                    "gw_sub": order["gateway_order_id"],
                    "method": order["payment_method"],
                    "period_end": period_end,
                },
            )

    new_balance = await grant_credit_lot(
        conn,
        user_id=order["user_id"],
        amount=int(order["credits_amount"]),
        source_type=source_type,
        transaction_type="purchase",
        description=f"Compra aprovada: {plan.get('name', order['plan_code'])} (+{order['credits_amount']} créditos)",
        order_id=order_id,
        expires_in_days=expires_in_days,
    )
    await conn.commit()

    broadcast_order_sse(
        str(order_id),
        {
            "event": "payment_confirmed",
            "order_id": str(order_id),
            "status": "paid",
            "credits_added": int(order["credits_amount"]),
            "new_balance": new_balance,
        },
    )
    return updated_order


async def get_order_by_id(
    conn: psycopg.AsyncConnection, order_id: UUID
) -> Optional[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM app_saas.orders WHERE id = %(id)s LIMIT 1;", {"id": order_id})
        return await cur.fetchone()


async def get_wallet_overview(
    conn: psycopg.AsyncConnection, user_id: UUID
) -> dict:
    """Return user's active credit balance, FIFO credit lots, transaction ledger, and orders."""
    balance = await sync_wallet_balance(conn, user_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, source_type, initial_amount, remaining_amount, expires_at, created_at
            FROM app_saas.credit_lots
            WHERE user_id = %(user_id)s
              AND remaining_amount > 0
              AND (expires_at IS NULL OR expires_at > clock_timestamp())
            ORDER BY expires_at ASC NULLS LAST, created_at ASC;
            """,
            {"user_id": user_id},
        )
        active_lots = await cur.fetchall()

        await cur.execute(
            """
            SELECT id, transaction_type, amount, balance_after, description, created_at
            FROM app_saas.credit_transactions
            WHERE user_id = %(user_id)s
            ORDER BY created_at DESC
            LIMIT 100;
            """,
            {"user_id": user_id},
        )
        transactions = await cur.fetchall()

        await cur.execute(
            """
            SELECT *
            FROM app_saas.orders
            WHERE user_id = %(user_id)s
            ORDER BY created_at DESC
            LIMIT 50;
            """,
            {"user_id": user_id},
        )
        orders = await cur.fetchall()

        await cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM app_saas.subscriptions
                WHERE user_id = %(user_id)s AND status = 'active'
            ) AS has_sub;
            """,
            {"user_id": user_id},
        )
        sub_row = await cur.fetchone()

    return {
        "credit_balance": balance,
        "has_active_subscription": bool(sub_row and sub_row["has_sub"]),
        "active_lots": active_lots,
        "transactions": transactions,
        "orders": orders,
    }

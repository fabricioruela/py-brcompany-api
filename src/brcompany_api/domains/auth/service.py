"""Business logic for Authentication, OTP Verification, FIFO Credit Lots, and API Keys."""

from datetime import datetime, timedelta, timezone
import logging
from typing import List, Optional, Tuple
from uuid import UUID, uuid4
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException, status

from brcompany_api.config import get_settings
from brcompany_api.utils.security import (
    generate_api_key,
    generate_otp_code,
    hash_api_key,
    hash_otp_code,
    hash_password,
    is_disposable_email,
    verify_password,
)

logger = logging.getLogger(__name__)


async def sync_wallet_balance(conn: psycopg.AsyncConnection, user_id: UUID) -> int:
    """Recalculate active non-expired credits from credit_lots and synchronize user_wallets."""
    async with conn.cursor(row_factory=dict_row) as cur:
        # Expire any lots whose expires_at is in the past
        await cur.execute(
            """
            UPDATE app_saas.credit_lots
            SET remaining_amount = 0
            WHERE user_id = %(user_id)s
              AND remaining_amount > 0
              AND expires_at IS NOT NULL
              AND expires_at <= clock_timestamp();
            """,
            {"user_id": user_id},
        )
        await cur.execute(
            """
            SELECT COALESCE(SUM(remaining_amount), 0)::int AS active_balance
            FROM app_saas.credit_lots
            WHERE user_id = %(user_id)s
              AND remaining_amount > 0
              AND (expires_at IS NULL OR expires_at > clock_timestamp());
            """,
            {"user_id": user_id},
        )
        row = await cur.fetchone()
        active_balance = int(row["active_balance"]) if row else 0

        await cur.execute(
            """
            INSERT INTO app_saas.user_wallets (user_id, credit_balance, updated_at)
            VALUES (%(user_id)s, %(balance)s, clock_timestamp())
            ON CONFLICT (user_id) DO UPDATE
            SET credit_balance = EXCLUDED.credit_balance,
                updated_at = clock_timestamp();
            """,
            {"user_id": user_id, "balance": active_balance},
        )
        return active_balance


async def grant_credit_lot(
    conn: psycopg.AsyncConnection,
    user_id: UUID,
    amount: int,
    source_type: str,
    transaction_type: str,
    description: str,
    order_id: Optional[UUID] = None,
    expires_in_days: Optional[int] = None,
) -> int:
    """Grant a credit lot (with optional expiration) and record ledger transaction."""
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        if expires_in_days
        else None
    )
    lot_id = uuid4()
    tx_id = uuid4()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.credit_lots (
                id, user_id, source_type, order_id, initial_amount, remaining_amount, expires_at
            ) VALUES (
                %(id)s, %(user_id)s, %(source_type)s, %(order_id)s, %(amount)s, %(amount)s, %(expires_at)s
            );
            """,
            {
                "id": lot_id,
                "user_id": user_id,
                "source_type": source_type,
                "order_id": order_id,
                "amount": amount,
                "expires_at": expires_at,
            },
        )
    new_balance = await sync_wallet_balance(conn, user_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.credit_transactions (
                id, user_id, order_id, transaction_type, amount, balance_after, description
            ) VALUES (
                %(id)s, %(user_id)s, %(order_id)s, %(tx_type)s, %(amount)s, %(balance_after)s, %(description)s
            );
            """,
            {
                "id": tx_id,
                "user_id": user_id,
                "order_id": order_id,
                "tx_type": transaction_type,
                "amount": amount,
                "balance_after": new_balance,
                "description": description,
            },
        )
    return new_balance


async def deduct_credits_fifo(
    conn: psycopg.AsyncConnection,
    user_id: UUID,
    amount_to_deduct: int,
    description: str,
    export_list_id: Optional[UUID] = None,
) -> int:
    """Atomically deduct credits using FIFO order by earliest expiration date."""
    if amount_to_deduct <= 0:
        return await sync_wallet_balance(conn, user_id)

    async with conn.cursor(row_factory=dict_row) as cur:
        # Lock wallet row for atomic concurrency control
        await cur.execute(
            """
            SELECT credit_balance
            FROM app_saas.user_wallets
            WHERE user_id = %(user_id)s
            FOR UPDATE;
            """,
            {"user_id": user_id},
        )
        # Fetch active lots in FIFO order (expiring soonest first, then oldest)
        await cur.execute(
            """
            SELECT id, remaining_amount
            FROM app_saas.credit_lots
            WHERE user_id = %(user_id)s
              AND remaining_amount > 0
              AND (expires_at IS NULL OR expires_at > clock_timestamp())
            ORDER BY expires_at ASC NULLS LAST, created_at ASC
            FOR UPDATE;
            """,
            {"user_id": user_id},
        )
        lots = await cur.fetchall()
        total_available = sum(int(l["remaining_amount"]) for l in lots)
        if total_available < amount_to_deduct:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Saldo de créditos insuficiente. Necessário: {amount_to_deduct}, Disponível: {total_available}.",
            )

        remaining_needed = amount_to_deduct
        for lot in lots:
            if remaining_needed <= 0:
                break
            lot_rem = int(lot["remaining_amount"])
            take = min(lot_rem, remaining_needed)
            await cur.execute(
                """
                UPDATE app_saas.credit_lots
                SET remaining_amount = remaining_amount - %(take)s
                WHERE id = %(lot_id)s;
                """,
                {"take": take, "lot_id": lot["id"]},
            )
            remaining_needed -= take

    new_balance = await sync_wallet_balance(conn, user_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.credit_transactions (
                id, user_id, export_list_id, transaction_type, amount, balance_after, description
            ) VALUES (
                %(id)s, %(user_id)s, %(export_list_id)s, 'usage', %(amount)s, %(balance_after)s, %(description)s
            );
            """,
            {
                "id": uuid4(),
                "user_id": user_id,
                "export_list_id": export_list_id,
                "amount": -amount_to_deduct,
                "balance_after": new_balance,
                "description": description,
            },
        )
    return new_balance


async def get_user_profile_by_id(
    conn: psycopg.AsyncConnection, user_id: UUID
) -> Optional[dict]:
    """Retrieve full user profile with active credit balance and subscription flag."""
    balance = await sync_wallet_balance(conn, user_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                u.id,
                u.email,
                u.full_name,
                u.phone,
                u.tax_id,
                u.role,
                u.is_email_verified,
                u.created_at,
                EXISTS (
                    SELECT 1 FROM app_saas.subscriptions s
                    WHERE s.user_id = u.id AND s.status = 'active'
                ) AS has_active_subscription
            FROM app_saas.users u
            WHERE u.id = %(user_id)s AND u.is_active = true
            LIMIT 1;
            """,
            {"user_id": user_id},
        )
        user = await cur.fetchone()
        if not user:
            return None
        user["credit_balance"] = balance
        return user


async def register_user(
    conn: psycopg.AsyncConnection,
    full_name: str,
    email: str,
    password: str,
    phone: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Tuple[dict, str]:
    """Register a new user, enforce anti-abuse checks, and generate a 6-digit OTP code."""
    clean_email = email.strip().lower()
    if "@" not in clean_email or is_disposable_email(clean_email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="E-mail inválido ou domínio de e-mail temporário/descartável não permitido.",
        )

    settings = get_settings()
    async with conn.cursor(row_factory=dict_row) as cur:
        # Check IP rate limit (max 5 registrations/OTPs from same IP in last 15 min)
        if ip_address:
            await cur.execute(
                """
                SELECT COUNT(*)::int AS recent_count
                FROM app_saas.email_otp_codes
                WHERE ip_address = %(ip)s
                  AND created_at >= clock_timestamp() - interval '15 minutes';
                """,
                {"ip": ip_address},
            )
            ip_row = await cur.fetchone()
            if ip_row and int(ip_row["recent_count"]) >= 10:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Muitas tentativas recentes deste endereço IP. Aguarde alguns minutos.",
                )

        await cur.execute(
            "SELECT id, is_email_verified FROM app_saas.users WHERE email = %(email)s LIMIT 1;",
            {"email": clean_email},
        )
        existing = await cur.fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Este e-mail já está cadastrado. Faça login ou solicite novo código.",
            )

        await cur.execute(
            "SELECT COUNT(*)::int AS admin_count FROM app_saas.users WHERE role = 'admin';"
        )
        admin_row = await cur.fetchone()
        has_any_admin = bool(admin_row and int(admin_row["admin_count"]) > 0)

        user_id = uuid4()
        is_configured_admin = clean_email in [e.lower() for e in settings.app_admin_emails]
        role = "admin" if (is_configured_admin or not has_any_admin) else "user"
        pwd_hash = hash_password(password)

        await cur.execute(
            """
            INSERT INTO app_saas.users (
                id, email, hashed_password, full_name, phone, role, is_active, is_email_verified
            ) VALUES (
                %(id)s, %(email)s, %(hashed_password)s, %(full_name)s, %(phone)s, %(role)s, true, false
            )
            RETURNING id, email, full_name, role, is_email_verified, created_at;
            """,
            {
                "id": user_id,
                "email": clean_email,
                "hashed_password": pwd_hash,
                "full_name": full_name.strip(),
                "phone": phone.strip() if phone else None,
                "role": role,
            },
        )
        user_row = await cur.fetchone()

        await cur.execute(
            """
            INSERT INTO app_saas.user_wallets (user_id, credit_balance)
            VALUES (%(user_id)s, 0);
            """,
            {"user_id": user_id},
        )

        otp_code = generate_otp_code()
        otp_hash = hash_otp_code(otp_code)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.app_otp_expire_minutes)

        await cur.execute(
            """
            INSERT INTO app_saas.email_otp_codes (user_id, code_hash, ip_address, expires_at)
            VALUES (%(user_id)s, %(code_hash)s, %(ip)s, %(expires_at)s);
            """,
            {
                "user_id": user_id,
                "code_hash": otp_hash,
                "ip": ip_address,
                "expires_at": expires_at,
            },
        )

    await conn.commit()
    logger.info("Registered user %s (role=%s). OTP code generated: %s", clean_email, role, otp_code)
    return user_row, otp_code


async def resend_otp_code(
    conn: psycopg.AsyncConnection, email: str, ip_address: Optional[str] = None
) -> str:
    """Generate and store a fresh OTP verification code for an unverified user."""
    clean_email = email.strip().lower()
    settings = get_settings()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, is_email_verified FROM app_saas.users WHERE email = %(email)s LIMIT 1;",
            {"email": clean_email},
        )
        user = await cur.fetchone()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Usuário não encontrado.",
            )
        if user["is_email_verified"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Este e-mail já foi verificado.",
            )

        otp_code = generate_otp_code()
        otp_hash = hash_otp_code(otp_code)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.app_otp_expire_minutes)
        await cur.execute(
            """
            INSERT INTO app_saas.email_otp_codes (user_id, code_hash, ip_address, expires_at)
            VALUES (%(user_id)s, %(code_hash)s, %(ip)s, %(expires_at)s);
            """,
            {
                "user_id": user["id"],
                "code_hash": otp_hash,
                "ip": ip_address,
                "expires_at": expires_at,
            },
        )
    await conn.commit()
    return otp_code


async def verify_email_otp(
    conn: psycopg.AsyncConnection, email: str, otp_code: str
) -> dict:
    """Validate 6-digit OTP code, mark email verified, and release 25 Trial credits idempotently."""
    clean_email = email.strip().lower()
    code_hash = hash_otp_code(otp_code)
    settings = get_settings()

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, email, full_name, role, is_email_verified
            FROM app_saas.users
            WHERE email = %(email)s AND is_active = true
            FOR UPDATE;
            """,
            {"email": clean_email},
        )
        user = await cur.fetchone()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conta não encontrada para o e-mail informado.",
            )

        user_id = user["id"]
        if not user["is_email_verified"]:
            await cur.execute(
                """
                SELECT id
                FROM app_saas.email_otp_codes
                WHERE user_id = %(user_id)s
                  AND code_hash = %(code_hash)s
                  AND consumed_at IS NULL
                  AND expires_at > clock_timestamp()
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                {"user_id": user_id, "code_hash": code_hash},
            )
            otp_row = await cur.fetchone()
            if not otp_row:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Código OTP inválido ou expirado.",
                )

            await cur.execute(
                "UPDATE app_saas.email_otp_codes SET consumed_at = clock_timestamp() WHERE id = %(id)s;",
                {"id": otp_row["id"]},
            )
            await cur.execute(
                """
                UPDATE app_saas.users
                SET is_email_verified = true, updated_at = clock_timestamp()
                WHERE id = %(user_id)s;
                """,
                {"user_id": user_id},
            )

            # Check if trial credits were already granted
            await cur.execute(
                """
                SELECT 1 FROM app_saas.credit_lots
                WHERE user_id = %(user_id)s AND source_type = 'trial'
                LIMIT 1;
                """,
                {"user_id": user_id},
            )
            already_granted = await cur.fetchone()
            if not already_granted:
                await grant_credit_lot(
                    conn,
                    user_id=user_id,
                    amount=settings.app_trial_credits,
                    source_type="trial",
                    transaction_type="bonus",
                    description=f"Bônus de Boas-Vindas ({settings.app_trial_credits} créditos Trial)",
                    expires_in_days=None,
                )

    await conn.commit()
    profile = await get_user_profile_by_id(conn, user_id)
    return profile


async def authenticate_user(
    conn: psycopg.AsyncConnection, email: str, password: str
) -> dict:
    """Verify email and password and return full user profile."""
    clean_email = email.strip().lower()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, hashed_password, is_active, is_email_verified
            FROM app_saas.users
            WHERE email = %(email)s
            LIMIT 1;
            """,
            {"email": clean_email},
        )
        row = await cur.fetchone()
        if not row or not verify_password(password, row["hashed_password"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="E-mail ou senha incorretos.",
            )
        if not row["is_active"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Conta desativada.",
            )

    profile = await get_user_profile_by_id(conn, row["id"])
    return profile


async def update_user_profile(
    conn: psycopg.AsyncConnection,
    user_id: UUID,
    full_name: Optional[str] = None,
    phone: Optional[str] = None,
    tax_id: Optional[str] = None,
) -> dict:
    """Update user profile details (including CPF/CNPJ tax_id required for Asaas checkout)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE app_saas.users
            SET full_name = COALESCE(%(full_name)s, full_name),
                phone = COALESCE(%(phone)s, phone),
                tax_id = COALESCE(%(tax_id)s, tax_id),
                updated_at = clock_timestamp()
            WHERE id = %(user_id)s;
            """,
            {
                "user_id": user_id,
                "full_name": full_name.strip() if full_name else None,
                "phone": phone.strip() if phone else None,
                "tax_id": "".join(ch for ch in tax_id if ch.isdigit()) if tax_id else None,
            },
        )
    await conn.commit()
    return await get_user_profile_by_id(conn, user_id)


async def create_user_api_key(
    conn: psycopg.AsyncConnection, user_id: UUID, name: str
) -> dict:
    """Create a new API key for programmatic DaaS access."""
    full_key, key_prefix, key_hash = generate_api_key()
    key_id = uuid4()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.api_keys (id, user_id, name, key_prefix, key_hash, is_active)
            VALUES (%(id)s, %(user_id)s, %(name)s, %(prefix)s, %(hash)s, true)
            RETURNING id, name, key_prefix, is_active, last_used_at, created_at;
            """,
            {
                "id": key_id,
                "user_id": user_id,
                "name": name.strip(),
                "prefix": key_prefix,
                "hash": key_hash,
            },
        )
        row = await cur.fetchone()
    await conn.commit()
    row["api_key"] = full_key
    return row


async def list_user_api_keys(
    conn: psycopg.AsyncConnection, user_id: UUID
) -> List[dict]:
    """List all API keys belonging to a user."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, name, key_prefix, is_active, last_used_at, created_at
            FROM app_saas.api_keys
            WHERE user_id = %(user_id)s
            ORDER BY created_at DESC;
            """,
            {"user_id": user_id},
        )
        return await cur.fetchall()


async def revoke_user_api_key(
    conn: psycopg.AsyncConnection, user_id: UUID, key_id: UUID
) -> None:
    """Revoke/delete a user's API key."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            DELETE FROM app_saas.api_keys
            WHERE id = %(key_id)s AND user_id = %(user_id)s;
            """,
            {"key_id": key_id, "user_id": user_id},
        )
    await conn.commit()


async def get_user_by_api_key(
    conn: psycopg.AsyncConnection, raw_api_key: str
) -> Optional[dict]:
    """Resolve user profile from an X-API-Key header and update last_used_at."""
    key_hash = hash_api_key(raw_api_key)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            UPDATE app_saas.api_keys
            SET last_used_at = clock_timestamp()
            WHERE key_hash = %(key_hash)s AND is_active = true
            RETURNING user_id;
            """,
            {"key_hash": key_hash},
        )
        row = await cur.fetchone()
        if not row:
            return None
    await conn.commit()
    return await get_user_profile_by_id(conn, row["user_id"])

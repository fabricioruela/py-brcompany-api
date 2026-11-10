"""Platform Administrator domain: global settings (including export deduplication toggle) and LGPD oversight."""

from datetime import datetime
import json
from typing import Annotated, List, Optional
from uuid import UUID
import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from brcompany_api.db import get_db_conn
from brcompany_api.domains.auth.dependencies import require_admin_user

router = APIRouter(prefix="/admin", tags=["Platform Administration"])


class PlatformSettingsResponse(BaseModel):
    export_deduplication_enabled: bool
    updated_at: Optional[datetime] = None
    total_users: int = 0
    total_exports: int = 0
    total_revenue_cents: int = 0
    active_opt_outs_count: int = 0


class UpdatePlatformSettingsRequest(BaseModel):
    export_deduplication_enabled: bool


class OptOutItem(BaseModel):
    cnpj: str
    requester_name: str
    requester_email: str
    requester_role: Optional[str] = None
    reason: Optional[str] = None
    status: str
    cloudflare_purged: bool = False
    created_at: datetime


async def get_export_deduplication_enabled(conn: psycopg.AsyncConnection) -> bool:
    """Read platform-wide export deduplication setting (controlled by platform Administrator)."""
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT value_json
                FROM app_saas.platform_settings
                WHERE key = 'export_deduplication_enabled'
                LIMIT 1;
                """
            )
            row = await cur.fetchone()
            if not row:
                return True
            val = row["value_json"]
            if isinstance(val, bool):
                return val
            if isinstance(val, dict):
                return bool(val.get("enabled", True))
            return str(val).lower() in ("true", "1", "yes")
    except Exception:
        return True


async def set_export_deduplication_enabled(
    conn: psycopg.AsyncConnection, enabled: bool, admin_user_id: UUID
) -> datetime:
    """Update platform-wide export deduplication setting."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.platform_settings (key, value_json, updated_by, updated_at)
            VALUES ('export_deduplication_enabled', %(val)s::jsonb, %(admin_id)s, clock_timestamp())
            ON CONFLICT (key) DO UPDATE
            SET value_json = EXCLUDED.value_json,
                updated_by = EXCLUDED.updated_by,
                updated_at = clock_timestamp()
            RETURNING updated_at;
            """,
            {"val": json.dumps(bool(enabled)), "admin_id": admin_user_id},
        )
        row = await cur.fetchone()
    await conn.commit()
    return row["updated_at"]


@router.get(
    "/settings",
    response_model=PlatformSettingsResponse,
    summary="Get Platform Settings & Metrics (Admin Only)",
)
async def get_settings_endpoint(
    _admin: Annotated[dict, Depends(require_admin_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    dedup_enabled = await get_export_deduplication_enabled(conn)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                (SELECT updated_at FROM app_saas.platform_settings WHERE key = 'export_deduplication_enabled' LIMIT 1) AS updated_at,
                (SELECT COUNT(*)::int FROM app_saas.users) AS total_users,
                (SELECT COUNT(*)::int FROM app_saas.export_lists) AS total_exports,
                (SELECT COALESCE(SUM(amount_cents), 0)::int FROM app_saas.orders WHERE status = 'paid') AS total_revenue_cents,
                (SELECT COUNT(*)::int FROM app_saas.company_opt_outs WHERE status = 'active') AS active_opt_outs_count;
            """
        )
        stats = await cur.fetchone() or {}

    return PlatformSettingsResponse(
        export_deduplication_enabled=dedup_enabled,
        updated_at=stats.get("updated_at"),
        total_users=stats.get("total_users", 0),
        total_exports=stats.get("total_exports", 0),
        total_revenue_cents=stats.get("total_revenue_cents", 0),
        active_opt_outs_count=stats.get("active_opt_outs_count", 0),
    )


@router.put(
    "/settings",
    response_model=PlatformSettingsResponse,
    summary="Enable or Disable Global Export Deduplication (Admin Only)",
)
async def update_settings_endpoint(
    body: UpdatePlatformSettingsRequest,
    admin: Annotated[dict, Depends(require_admin_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    await set_export_deduplication_enabled(
        conn,
        enabled=body.export_deduplication_enabled,
        admin_user_id=admin["id"],
    )
    return await get_settings_endpoint(admin, conn)


@router.get(
    "/opt-outs",
    response_model=List[OptOutItem],
    summary="List LGPD Opt-Out Requests (Admin Only)",
)
async def list_opt_outs(
    _admin: Annotated[dict, Depends(require_admin_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT cnpj, requester_name, requester_email, requester_role, reason, status, cloudflare_purged, created_at
            FROM app_saas.company_opt_outs
            ORDER BY created_at DESC
            LIMIT 200;
            """
        )
        rows = await cur.fetchall()
        return [OptOutItem(**r) for r in rows]

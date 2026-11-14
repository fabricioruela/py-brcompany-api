"""Platform Administrator domain: global settings (including export deduplication toggle) and LGPD oversight."""

from datetime import datetime
import json
from typing import Annotated, List, Literal, Optional
from uuid import UUID
import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from brcompany_api.config import Settings, get_settings
from brcompany_api.db import get_db_conn
from brcompany_api.domains.auth.dependencies import require_admin_user
from brcompany_api.domains.geo.google_geocoding import admin_geocode_companies
from brcompany_api.domains.search.service import invalidate_search_cache

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


class AdminGeocodeRequest(BaseModel):
    cnpjs: List[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Lista de CNPJs (14 dígitos) para atualização forçada via Google Geocoding API (máximo 50 por lote).",
    )


class AdminGeocodeCompanyResult(BaseModel):
    cnpj: str
    status: Literal[
        "updated",
        "skipped_low_precision",
        "skipped_incomplete_address",
        "not_found",
        "error",
    ]
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    precision: Optional[str] = None
    source: Optional[str] = None
    google_location_type: Optional[str] = None
    formatted_address: Optional[str] = None
    query_address: Optional[str] = None
    message: Optional[str] = None


class AdminBatchGeocodeResponse(BaseModel):
    requested: int
    updated: int
    skipped_low_precision: int
    skipped_incomplete_address: int
    not_found: int
    errors: int
    results: List[AdminGeocodeCompanyResult]


@router.post(
    "/geo/geocode-companies",
    response_model=AdminBatchGeocodeResponse,
    summary="Force Georeferencing via Google Geocoding API (Admin Only)",
    description=(
        "Consulta a Google Geocoding API usando logradouro, número, município e UF "
        "para até 50 CNPJs e atualiza geo.addresses + app_saas.company_geo_cache."
    ),
)
async def force_geocode_companies(
    body: AdminGeocodeRequest,
    _admin: Annotated[dict, Depends(require_admin_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    api_key = (settings.app_google_geocoding_api_key or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APP_GOOGLE_GEOCODING_API_KEY não configurada no servidor.",
        )

    cleaned_cnpjs = []
    seen = set()
    for raw in body.cnpjs:
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(digits) == 14 and digits not in seen:
            seen.add(digits)
            cleaned_cnpjs.append(digits)

    if not cleaned_cnpjs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Informe ao menos um CNPJ válido de 14 dígitos.",
        )

    raw_results = await admin_geocode_companies(
        conn,
        cleaned_cnpjs,
        api_key=api_key,
        timeout_seconds=settings.app_google_geocoding_timeout_seconds,
    )
    results = [AdminGeocodeCompanyResult(**item) for item in raw_results]

    updated_count = sum(1 for r in results if r.status == "updated")
    if updated_count > 0:
        invalidate_search_cache()

    return AdminBatchGeocodeResponse(
        requested=len(cleaned_cnpjs),
        updated=updated_count,
        skipped_low_precision=sum(1 for r in results if r.status == "skipped_low_precision"),
        skipped_incomplete_address=sum(1 for r in results if r.status == "skipped_incomplete_address"),
        not_found=sum(1 for r in results if r.status == "not_found"),
        errors=sum(1 for r in results if r.status == "error"),
        results=results,
    )

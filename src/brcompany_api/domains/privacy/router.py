"""LGPD Privacy & Opt-Out domain with optional Cloudflare Edge Cache purge."""

import logging
from typing import Annotated, Optional
import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from brcompany_api.config import Settings, get_settings
from brcompany_api.db import get_db_conn
from brcompany_api.domains.search.service import invalidate_search_cache
from brcompany_api.utils.cnpj import clean_cnpj

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/privacy", tags=["LGPD Privacy & Opt-Out"])


class OptOutRequest(BaseModel):
    cnpj: str = Field(..., description="14-digit CNPJ to remove from public pages and search")
    requester_name: str = Field(..., min_length=2, max_length=255)
    requester_email: str = Field(..., min_length=5, max_length=255)
    requester_role: Optional[str] = Field(default="Sócio / Representante Legal", max_length=100)
    reason: Optional[str] = Field(default="Solicitação de Opt-Out LGPD", max_length=1000)


class OptOutResponse(BaseModel):
    cnpj: str
    status: str
    cloudflare_purged: bool
    message: str


async def is_cnpj_opted_out(conn: psycopg.AsyncConnection, cnpj: str) -> bool:
    """Return True if the 14-digit CNPJ has an active LGPD opt-out record."""
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT 1
                FROM app_saas.company_opt_outs
                WHERE cnpj = %(cnpj)s AND status = 'active'
                LIMIT 1;
                """,
                {"cnpj": cnpj},
            )
            return (await cur.fetchone()) is not None
    except Exception:
        return False


async def _purge_cloudflare_cache(cnpj: str, settings: Settings) -> bool:
    """Purge public SEO URLs on Cloudflare Edge Cache if credentials are configured."""
    if not settings.app_cloudflare_zone_id or not settings.app_cloudflare_api_token:
        return False
    url = f"https://api.cloudflare.com/client/v4/zones/{settings.app_cloudflare_zone_id}/purge_cache"
    target_urls = [
        f"{settings.app_public_site_url.rstrip('/')}/empresa/{cnpj}",
    ]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.app_cloudflare_api_token}"},
                json={"files": target_urls},
            )
            return resp.status_code == 200
    except Exception as exc:
        logger.warning("Cloudflare cache purge failed for %s: %s", cnpj, exc)
        return False


@router.post(
    "/opt-out",
    response_model=OptOutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request Immediate LGPD Opt-Out Removal for a CNPJ",
)
async def request_opt_out(
    body: OptOutRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    cleaned = clean_cnpj(body.cnpj)
    if len(cleaned) != 14:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Informe o CNPJ completo com 14 dígitos para solicitar a remoção.",
        )

    purged = await _purge_cloudflare_cache(cleaned, settings)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.company_opt_outs (
                cnpj, requester_name, requester_email, requester_role, reason, status, cloudflare_purged
            ) VALUES (
                %(cnpj)s, %(name)s, %(email)s, %(role)s, %(reason)s, 'active', %(purged)s
            )
            ON CONFLICT (cnpj) DO UPDATE
            SET requester_name = EXCLUDED.requester_name,
                requester_email = EXCLUDED.requester_email,
                requester_role = EXCLUDED.requester_role,
                reason = EXCLUDED.reason,
                status = 'active',
                cloudflare_purged = EXCLUDED.cloudflare_purged,
                updated_at = clock_timestamp();
            """,
            {
                "cnpj": cleaned,
                "name": body.requester_name.strip(),
                "email": body.requester_email.strip().lower(),
                "role": body.requester_role,
                "reason": body.reason,
                "purged": purged,
            },
        )
    await conn.commit()
    invalidate_search_cache()
    return OptOutResponse(
        cnpj=cleaned,
        status="active",
        cloudflare_purged=purged,
        message="Solicitação registrada com sucesso. A exibição pública e nas buscas foi desativada imediatamente.",
    )

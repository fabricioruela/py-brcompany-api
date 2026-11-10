"""FastAPI router for company and establishment endpoints (with SEO contact masking, 1-credit DaaS unlock, and LGPD Opt-Out)."""

import asyncio
from typing import Annotated, Optional
import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from brcompany_api.db import get_db_conn
from brcompany_api.domains.admin.router import get_export_deduplication_enabled
from brcompany_api.domains.auth.dependencies import get_optional_user
from brcompany_api.domains.auth.service import deduct_credits_fifo
from brcompany_api.domains.company import service
from brcompany_api.domains.company.schemas import (
    BranchListResponse,
    CompanyProfileResponse,
    EstablishmentSummary,
    PartnerItem,
    PartnerListResponse,
)
from brcompany_api.domains.privacy.router import is_cnpj_opted_out
from brcompany_api.utils.cnpj import clean_cnpj, validate_cnpj
from brcompany_api.utils.masking import mask_cpf_or_tax_id, mask_email, mask_phone, mask_street

router = APIRouter(prefix="/companies", tags=["Companies & Establishments"])


def _apply_contact_and_lgpd_masking(profile: CompanyProfileResponse) -> CompanyProfileResponse:
    """Mask phone/email on public pages, mask partner CPFs, and hide MEI personal email/residential street."""
    for p in profile.partners:
        p.partner_tax_id = mask_cpf_or_tax_id(p.partner_tax_id)
        p.legal_representative_tax_id = mask_cpf_or_tax_id(p.legal_representative_tax_id)

    est = profile.establishment
    masked_phone = mask_phone(est.area_code_1, est.phone_1)
    est.area_code_1 = None
    est.phone_1 = masked_phone

    if profile.company.is_mei_optant:
        # LGPD Art. 7: Hide personal email and residential street number of MEI on public pages
        est.email = None
        est.street = mask_street(est.street_type, est.street, est.number)
        est.number = "***"
        est.complement = None
    else:
        est.email = mask_email(est.email)

    profile.contacts_locked = True
    profile.credits_charged = 0
    return profile


@router.get(
    "/{cnpj_base}/branches",
    response_model=BranchListResponse,
    summary="List Establishments / Branches",
    description="Retrieve all establishments/branches registered under the given 8-digit base CNPJ.",
)
async def list_branches(
    cnpj_base: Annotated[str, Path(description="8-digit base CNPJ")],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    limit: Annotated[int, Query(ge=1, le=200, description="Page limit")] = 50,
    offset: Annotated[int, Query(ge=0, description="Page offset")] = 0,
):
    cleaned = clean_cnpj(cnpj_base)
    if len(cleaned) != 8:
        if len(cleaned) == 14:
            cleaned = cleaned[:8]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="CNPJ base must contain 8 digits",
            )

    async with service.maybe_pipeline(conn):
        total, branch_rows = await asyncio.gather(
            service.count_branches(conn, cleaned),
            service.get_branches_by_base(conn, cleaned, limit=limit, offset=offset),
        )

    return BranchListResponse(
        cnpj_base=cleaned,
        total=total,
        branches=[EstablishmentSummary.model_validate(b) for b in branch_rows],
    )


@router.get(
    "/{cnpj_base}/partners",
    response_model=PartnerListResponse,
    summary="List Partners (QSA)",
    description="Retrieve corporate shareholders and administrators (Quadro de Sócios e Administradores) for the given 8-digit base CNPJ.",
)
async def list_partners(
    cnpj_base: Annotated[str, Path(description="8-digit base CNPJ")],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    cleaned = clean_cnpj(cnpj_base)
    if len(cleaned) != 8:
        if len(cleaned) == 14:
            cleaned = cleaned[:8]
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="CNPJ base must contain 8 digits",
            )

    partner_rows = await service.get_partners_by_base(conn, cleaned)

    return PartnerListResponse(
        cnpj_base=cleaned,
        total=len(partner_rows),
        partners=[PartnerItem.model_validate(p) for p in partner_rows],
    )


@router.get(
    "/{cnpj:path}",
    response_model=CompanyProfileResponse,
    summary="Get Consolidated Company Profile (Public SEO Masked or 1-Credit Unmasked via API Key / Unlock)",
    description="Retrieve company registration details, headquarters/branch establishment, CNAEs, Simples/MEI, and QSA partners by 14-digit CNPJ or 8-digit CNPJ base.",
)
async def get_company(
    cnpj: Annotated[str, Path(description="14-digit CNPJ or 8-digit CNPJ base (with or without mask)")],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    unlock: Annotated[bool, Query(description="Set to true when authenticated to spend 1 credit and unlock full contacts")] = False,
    current_user: Annotated[Optional[dict], Depends(get_optional_user)] = None,
):
    cleaned = clean_cnpj(cnpj)
    if len(cleaned) not in (8, 14):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CNPJ must contain either 14 digits (full establishment) or 8 digits (base company)",
        )

    if len(cleaned) == 14 and not validate_cnpj(cleaned):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"CNPJ check digits are invalid for {cnpj}",
        )

    profile = await service.get_company_profile(conn, cleaned)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company or establishment not found for CNPJ {cnpj}",
        )

    full_cnpj = profile.establishment.cnpj
    if await is_cnpj_opted_out(conn, full_cnpj):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Este registro foi removido da exibição pública por solicitação de privacidade (Opt-Out LGPD).",
        )

    # Always ensure partner CPFs have central digits masked
    for p in profile.partners:
        p.partner_tax_id = mask_cpf_or_tax_id(p.partner_tax_id)
        p.legal_representative_tax_id = mask_cpf_or_tax_id(p.legal_representative_tax_id)

    # Determine if user is requesting/entitled to full unmasked contacts:
    # 1. Authenticated via X-API-Key (DaaS request = 1 credit)
    # 2. Authenticated via JWT and either passed ?unlock=true OR already unlocked this CNPJ previously
    if current_user:
        user_id = current_user["id"]
        is_api_key = current_user.get("auth_method") == "api_key"
        dedup_enabled = await get_export_deduplication_enabled(conn)

        already_unlocked = False
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT 1 FROM app_saas.user_exported_cnpjs
                    WHERE user_id = %(user_id)s AND cnpj = %(cnpj)s
                    LIMIT 1;
                    """,
                    {"user_id": user_id, "cnpj": full_cnpj},
                )
                already_unlocked = (await cur.fetchone()) is not None
        except Exception:
            already_unlocked = False

        if already_unlocked and dedup_enabled:
            profile.contacts_locked = False
            profile.credits_charged = 0
            return profile

        if is_api_key or unlock:
            await deduct_credits_fifo(
                conn,
                user_id=user_id,
                amount_to_deduct=1,
                description=f"Desbloqueio/Consulta de CNPJ {full_cnpj} ({'API Key' if is_api_key else 'Painel Web'})",
            )
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO app_saas.user_exported_cnpjs (user_id, cnpj)
                    VALUES (%(user_id)s, %(cnpj)s)
                    ON CONFLICT (user_id, cnpj) DO NOTHING;
                    """,
                    {"user_id": user_id, "cnpj": full_cnpj},
                )
            await conn.commit()
            profile.contacts_locked = False
            profile.credits_charged = 1
            return profile

    return _apply_contact_and_lgpd_masking(profile)

"""FastAPI router for company and establishment endpoints."""

from typing import Annotated
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from brcompany_api.db import get_db_conn
from brcompany_api.domains.company import service
from brcompany_api.domains.company.schemas import (
    BranchListResponse,
    CompanyProfileResponse,
    EstablishmentSummary,
    PartnerItem,
    PartnerListResponse,
)
from brcompany_api.utils.cnpj import clean_cnpj, validate_cnpj

router = APIRouter(prefix="/companies", tags=["Companies & Establishments"])


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

    total = await service.count_branches(conn, cleaned)
    branch_rows = await service.get_branches_by_base(conn, cleaned, limit=limit, offset=offset)

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
    summary="Get Consolidated Company Profile",
    description="Retrieve company registration details, headquarters/branch establishment, CNAEs, Simples/MEI, and QSA partners by 14-digit CNPJ or 8-digit CNPJ base (with or without mask).",
)
async def get_company(
    cnpj: Annotated[str, Path(description="14-digit CNPJ or 8-digit CNPJ base (with or without mask)")],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    cleaned = clean_cnpj(cnpj)
    if len(cleaned) not in (8, 14):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CNPJ must contain either 14 digits (full establishment) or 8 digits (base company)",
        )

    # If full 14 digits provided, validate check digits
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

    return profile

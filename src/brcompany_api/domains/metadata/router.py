"""FastAPI router for domain metadata and system status."""

from typing import Annotated, Optional
import psycopg
from fastapi import APIRouter, Depends, Query

from brcompany_api.config import Settings, get_settings
from brcompany_api.db import get_db_conn
from brcompany_api.domains.metadata import service
from brcompany_api.domains.metadata.schemas import (
    DomainItem,
    DomainListResponse,
    EtlStatusResponse,
)

router = APIRouter(tags=["Metadata & Domains"])


@router.get(
    "/domains/cnaes",
    response_model=DomainListResponse,
    summary="List CNAE Economic Activities",
    description="Retrieve CNAE activity codes and descriptions, with optional search query.",
)
async def list_cnaes(
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    q: Annotated[Optional[str], Query(description="Search term in code or description")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Max items to return")] = 100,
):
    rows = await service.get_cnaes(conn, q=q, limit=limit)
    return DomainListResponse(
        total=len(rows),
        items=[DomainItem.model_validate(r) for r in rows],
    )


@router.get(
    "/domains/municipalities",
    response_model=DomainListResponse,
    summary="List Municipalities",
    description="Retrieve Brazilian municipalities codes and names, with optional search query.",
)
async def list_municipalities(
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    q: Annotated[Optional[str], Query(description="Search term in code or municipality name")] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Max items to return")] = 100,
):
    rows = await service.get_municipalities(conn, q=q, limit=limit)
    return DomainListResponse(
        total=len(rows),
        items=[DomainItem.model_validate(r) for r in rows],
    )


@router.get(
    "/domains/legal-natures",
    response_model=DomainListResponse,
    summary="List Legal Natures",
    description="Retrieve all Brazilian legal nature classifications.",
)
async def list_legal_natures(
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    rows = await service.get_legal_natures(conn)
    return DomainListResponse(
        total=len(rows),
        items=[DomainItem.model_validate(r) for r in rows],
    )


@router.get(
    "/status",
    response_model=EtlStatusResponse,
    summary="ETL Sync & Schema Status",
    description="Get metadata about the current active schema, last published Receita Federal sync month, and row counts.",
)
async def get_system_status(
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    status_data = await service.get_etl_status(conn, settings.app_db_schema)
    return EtlStatusResponse.model_validate(status_data)

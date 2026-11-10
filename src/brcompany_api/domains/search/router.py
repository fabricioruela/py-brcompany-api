"""FastAPI router for B2B Advanced Search Count and Masked Preview."""

import time
from typing import Annotated
import psycopg
from fastapi import APIRouter, Depends

from brcompany_api.db import get_db_conn
from brcompany_api.domains.search import service
from brcompany_api.domains.search.schemas import (
    SearchCountResponse,
    SearchFilterRequest,
    SearchPreviewResponse,
)

router = APIRouter(prefix="/search", tags=["B2B Search Engine"])


@router.post(
    "/count",
    response_model=SearchCountResponse,
    summary="Fast Market Count for Selected Filters",
    description="Returns total count of active Brazilian companies matching the B2B prospecting filters.",
)
async def search_count(
    filters: SearchFilterRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    t0 = time.perf_counter()
    total = await service.count_matching_companies(conn, filters)
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return SearchCountResponse(total_companies=total, execution_time_ms=elapsed_ms)


@router.post(
    "/preview",
    response_model=SearchPreviewResponse,
    summary="Free 5-Row Masked Preview & Total Count",
    description="Returns total count and the first 5 matching companies with masked contact data.",
)
async def search_preview(
    filters: SearchFilterRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    t0 = time.perf_counter()
    total = await service.count_matching_companies(conn, filters)
    items = await service.preview_matching_companies(conn, filters, limit=5) if total > 0 else []
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return SearchPreviewResponse(
        total_companies=total,
        preview_count=len(items),
        items=items,
        execution_time_ms=elapsed_ms,
    )

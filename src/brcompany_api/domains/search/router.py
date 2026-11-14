"""FastAPI router for B2B Advanced Search Count and Masked Preview."""

import time
from typing import Annotated
import psycopg
from fastapi import APIRouter, Depends, Query

from brcompany_api.config import Settings, get_settings
from brcompany_api.db import get_db_conn
from brcompany_api.domains.search import service
from brcompany_api.domains.search.schemas import (
    MapSearchResponse,
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
    settings: Annotated[Settings, Depends(get_settings)],
):
    t0 = time.perf_counter()
    total = await service.get_cached_count(
        conn,
        filters,
        ttl_seconds=settings.app_search_cache_ttl_seconds,
        max_entries=settings.app_search_cache_max_entries,
    )
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
    settings: Annotated[Settings, Depends(get_settings)],
):
    t0 = time.perf_counter()
    total, items = await service.get_cached_preview(
        conn,
        filters,
        limit=5,
        ttl_seconds=settings.app_search_cache_ttl_seconds,
        max_entries=settings.app_search_cache_max_entries,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return SearchPreviewResponse(
        total_companies=total,
        preview_count=len(items),
        items=items,
        execution_time_ms=elapsed_ms,
    )


@router.post(
    "/map",
    response_model=MapSearchResponse,
    summary="Georeferenced Map Preview (Max 100 Masked Pins)",
    description="Returns total count and up to 100 georeferenced companies matching filters (including optional radius/bounds) with masked contacts.",
)
async def search_map(
    filters: SearchFilterRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=100, description="Max mapped pins (capped at 100)")] = 100,
):
    t0 = time.perf_counter()
    total, items, center = await service.get_cached_map_preview(
        conn,
        filters,
        limit=limit,
        ttl_seconds=settings.app_search_cache_ttl_seconds,
        max_entries=settings.app_search_cache_max_entries,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return MapSearchResponse(
        total_companies=total,
        mapped_count=len(items),
        limit=min(max(limit, 1), 100),
        center=center,
        items=items,
        execution_time_ms=elapsed_ms,
    )

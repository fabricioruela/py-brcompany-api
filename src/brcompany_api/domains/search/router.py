"""FastAPI router for B2B Advanced Search Count and Masked Preview."""

import math
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
    service.validate_required_location_filters(filters)
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
    summary="Paginated Masked Preview & Total Count",
    description="Returns total count and paginated matching companies with masked contact data (no total result cap).",
)
async def search_preview(
    filters: SearchFilterRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    page: Annotated[int, Query(ge=1, description="Page number (1-indexed)")] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, description="Items per page")] = 50,
):
    service.validate_required_location_filters(filters)
    t0 = time.perf_counter()
    total, items = await service.get_cached_preview(
        conn,
        filters,
        page=page,
        page_size=page_size,
        ttl_seconds=settings.app_search_cache_ttl_seconds,
        max_entries=settings.app_search_cache_max_entries,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    total_pages = math.ceil(total / page_size) if total > 0 else 0
    return SearchPreviewResponse(
        total_companies=total,
        preview_count=len(items),
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        items=items,
        execution_time_ms=elapsed_ms,
    )


@router.post(
    "/map",
    response_model=MapSearchResponse,
    summary="Georeferenced Map Explorer (Progressive Pins & Server Clusters)",
    description="Returns total count and either progressive batches of georeferenced companies or server-side aggregated clusters for massive result sets.",
)
async def search_map(
    filters: SearchFilterRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=2000, description="Batch size per request for progressive loading")] = 500,
    offset: Annotated[int, Query(ge=0, description="Pagination offset for progressive pin loading")] = 0,
):
    service.validate_required_location_filters(filters)
    t0 = time.perf_counter()
    total, mode, items, clusters, has_more, next_offset, center = await service.get_cached_map_preview(
        conn,
        filters,
        limit=limit,
        offset=offset,
        ttl_seconds=settings.app_search_cache_ttl_seconds,
        max_entries=settings.app_search_cache_max_entries,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    mapped_count = len(items) if mode == "pins" else sum(c.count for c in clusters)
    return MapSearchResponse(
        total_companies=total,
        mapped_count=mapped_count,
        limit=min(max(limit, 1), 2000),
        offset=offset,
        has_more=has_more,
        next_offset=next_offset,
        mode=mode,
        center=center,
        items=items,
        clusters=clusters,
        execution_time_ms=elapsed_ms,
    )

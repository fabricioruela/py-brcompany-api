"""FastAPI router for B2B Advanced Search Count and Masked Preview."""

import math
import time
from typing import Annotated, Optional
import psycopg
from fastapi import APIRouter, Depends, Query

from brcompany_api.config import Settings, get_settings
from brcompany_api.db import get_db_conn
from brcompany_api.domains.search import service
from brcompany_api.domains.search.schemas import (
    GeoBoundsFilter,
    MapPointCompaniesRequest,
    MapPointCompaniesResponse,
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
    summary="Georeferenced Map Explorer (Progressive Pins, Coordinate Groups & Server Clusters)",
    description="Returns total count and either progressive batches of georeferenced companies, zoom-aware server clusters, or exact-coordinate grouped pins.",
)
async def search_map(
    filters: SearchFilterRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: Annotated[int, Query(ge=1, le=2000, description="Batch size per request for progressive loading")] = 500,
    offset: Annotated[int, Query(ge=0, description="Pagination offset for progressive pin loading")] = 0,
    zoom: Annotated[Optional[int], Query(ge=1, le=22, description="Current Google Maps zoom level")] = None,
    viewport_north: Annotated[Optional[float], Query(ge=-90.0, le=90.0)] = None,
    viewport_south: Annotated[Optional[float], Query(ge=-90.0, le=90.0)] = None,
    viewport_east: Annotated[Optional[float], Query(ge=-180.0, le=180.0)] = None,
    viewport_west: Annotated[Optional[float], Query(ge=-180.0, le=180.0)] = None,
):
    service.validate_required_location_filters(filters)
    viewport_bounds = (
        GeoBoundsFilter(
            north=viewport_north,
            south=viewport_south,
            east=viewport_east,
            west=viewport_west,
        )
        if None not in (viewport_north, viewport_south, viewport_east, viewport_west)
        else None
    )
    t0 = time.perf_counter()
    raw_result = await service.get_cached_map_preview(
        conn,
        filters,
        limit=limit,
        offset=offset,
        ttl_seconds=settings.app_search_cache_ttl_seconds,
        max_entries=settings.app_search_cache_max_entries,
        zoom=zoom,
        viewport_bounds=viewport_bounds,
    )
    if len(raw_result) == 7:
        total, mode, items, clusters, has_more, next_offset, center = raw_result
        coordinate_groups = []
    else:
        total, mode, items, clusters, coordinate_groups, has_more, next_offset, center = raw_result
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    if mode == "pins":
        mapped_count = len(items)
    elif mode == "coordinate_pins":
        mapped_count = sum(g.count for g in coordinate_groups)
    else:
        mapped_count = sum(c.count for c in clusters)
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
        coordinate_groups=coordinate_groups,
        execution_time_ms=elapsed_ms,
    )


@router.post(
    "/map/point-companies",
    response_model=MapPointCompaniesResponse,
    summary="Paginated Companies at a Dense Map Coordinate",
    description="Returns paginated companies sharing a specific coordinate pin on the map, with optional search filtering.",
)
async def search_map_point_companies(
    payload: MapPointCompaniesRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    service.validate_required_location_filters(payload.filters)
    t0 = time.perf_counter()
    total, items = await service.search_map_point_companies(
        conn,
        payload.filters,
        latitude=payload.latitude,
        longitude=payload.longitude,
        postal_codes=payload.postal_codes,
        q_point=payload.q_point,
        page=payload.page,
        page_size=payload.page_size,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    has_more = (payload.page * payload.page_size) < total
    return MapPointCompaniesResponse(
        total_companies=total,
        page=payload.page,
        page_size=payload.page_size,
        has_more=has_more,
        items=items,
        execution_time_ms=elapsed_ms,
    )

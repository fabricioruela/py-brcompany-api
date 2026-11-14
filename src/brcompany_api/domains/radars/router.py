"""Saved Radars domain: Weekly Niche Alerts for Assinatura Smart subscribers."""

from datetime import date, datetime, timedelta
import json
from typing import Annotated, Any, Dict, List, Optional
from uuid import UUID, uuid4
import psycopg
from psycopg.rows import dict_row
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from brcompany_api.db import get_db_conn
from brcompany_api.domains.auth.dependencies import get_current_user
from brcompany_api.domains.search.schemas import SearchFilterRequest
from brcompany_api.domains.search.service import (
    count_matching_companies,
    validate_required_location_filters,
)

router = APIRouter(prefix="/radars", tags=["Saved Niche Radars (Smart Alerts)"])


class CreateRadarRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    filters: SearchFilterRequest


class SavedRadarItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    filters_json: Dict[str, Any]
    is_active: bool
    last_notified_at: Optional[datetime] = None
    last_known_count: int = 0
    new_companies_last_30d: int = 0
    created_at: datetime


class RadarListResponse(BaseModel):
    total: int
    items: List[SavedRadarItem]


@router.get(
    "",
    response_model=RadarListResponse,
    summary="List Saved Niche Radars",
)
async def list_radars(
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, name, filters_json, is_active, last_notified_at, last_known_count, created_at
            FROM app_saas.saved_radars
            WHERE user_id = %(user_id)s
            ORDER BY created_at DESC;
            """,
            {"user_id": current_user["id"]},
        )
        rows = await cur.fetchall()

    items: List[SavedRadarItem] = []
    thirty_days_ago = date.today() - timedelta(days=30)
    for r in rows:
        try:
            f_req = SearchFilterRequest.model_validate(r["filters_json"])
            f_recent = f_req.model_copy(update={"start_date_from": thirty_days_ago})
            recent_count = await count_matching_companies(conn, f_recent)
        except Exception:
            recent_count = 0
        items.append(
            SavedRadarItem(
                id=r["id"],
                name=r["name"],
                filters_json=r["filters_json"],
                is_active=r["is_active"],
                last_notified_at=r["last_notified_at"],
                last_known_count=r["last_known_count"] or 0,
                new_companies_last_30d=recent_count,
                created_at=r["created_at"],
            )
        )

    return RadarListResponse(total=len(items), items=items)


@router.post(
    "",
    response_model=SavedRadarItem,
    status_code=status.HTTP_201_CREATED,
    summary="Create Saved Radar for Weekly Niche Alerts",
)
async def create_radar(
    body: CreateRadarRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    validate_required_location_filters(body.filters)
    if not current_user.get("has_active_subscription") and current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Os Radares de Monitoramento Semanal são exclusivos da Assinatura Smart.",
        )

    total_now = await count_matching_companies(conn, body.filters)
    thirty_days_ago = date.today() - timedelta(days=30)
    recent_filters = body.filters.model_copy(update={"start_date_from": thirty_days_ago})
    recent_count = await count_matching_companies(conn, recent_filters)

    radar_id = uuid4()
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.saved_radars (
                id, user_id, name, filters_json, is_active, last_known_count
            ) VALUES (
                %(id)s, %(user_id)s, %(name)s, %(filters_json)s::jsonb, true, %(count)s
            )
            RETURNING id, name, filters_json, is_active, last_notified_at, last_known_count, created_at;
            """,
            {
                "id": radar_id,
                "user_id": current_user["id"],
                "name": body.name.strip(),
                "filters_json": json.dumps(body.filters.model_dump(mode="json")),
                "count": total_now,
            },
        )
        row = await cur.fetchone()
    await conn.commit()

    return SavedRadarItem(
        id=row["id"],
        name=row["name"],
        filters_json=row["filters_json"],
        is_active=row["is_active"],
        last_notified_at=row["last_notified_at"],
        last_known_count=row["last_known_count"] or 0,
        new_companies_last_30d=recent_count,
        created_at=row["created_at"],
    )


@router.delete(
    "/{radar_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Saved Radar",
)
async def delete_radar(
    radar_id: UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM app_saas.saved_radars WHERE id = %(id)s AND user_id = %(user_id)s;",
            {"id": radar_id, "user_id": current_user["id"]},
        )
    await conn.commit()

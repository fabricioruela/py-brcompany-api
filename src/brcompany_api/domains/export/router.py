"""FastAPI router for B2B Export Calculation, Atomic CSV/XLSX Download, and 30-Day History."""

from typing import Annotated, Literal
from uuid import UUID
import psycopg
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from brcompany_api.db import get_db_conn
from brcompany_api.domains.auth.dependencies import get_current_user
from brcompany_api.domains.export import service
from brcompany_api.domains.export.schemas import (
    ExportCalculateRequest,
    ExportCalculateResponse,
    ExportDownloadRequest,
    ExportHistoryResponse,
    ExportListHistoryItem,
)

router = APIRouter(prefix="/export", tags=["B2B Export & Lists History"])


@router.post(
    "/calculate",
    response_model=ExportCalculateResponse,
    summary="Calculate Net Credit Cost & Deduplication for Export",
)
async def export_calculate(
    body: ExportCalculateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    return await service.calculate_export_cost(conn, user_id=current_user["id"], req=body)


@router.post(
    "/download",
    summary="Deduct Credits Atomically & Download CSV/XLSX (Max 5,000 rows)",
)
async def export_download(
    body: ExportDownloadRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    list_id, _list_name, file_bytes = await service.execute_export_and_store_files(
        conn, user_id=current_user["id"], req=body
    )
    ext = "xlsx" if body.format == "xlsx" else "csv"
    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if ext == "xlsx"
        else "text/csv; charset=utf-8"
    )
    filename = f"panoramapj_leads_{str(list_id)[:8]}.{ext}"
    return Response(
        content=file_bytes,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-List-Id": str(list_id),
        },
    )


@router.get(
    "/history",
    response_model=ExportHistoryResponse,
    summary="List Exported Lists (30-Day Physical Retention)",
)
async def get_export_history(
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    rows = await service.list_user_export_history(conn, user_id=current_user["id"])
    return ExportHistoryResponse(
        total=len(rows),
        items=[ExportListHistoryItem.model_validate(r) for r in rows],
    )


@router.get(
    "/history/{list_id}/download",
    summary="Free Re-Download of Stored CSV/XLSX File (Within 30 Days)",
)
async def redownload_export_list(
    list_id: UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    format: Annotated[Literal["csv", "xlsx"], Query()] = "csv",
):
    _name, file_bytes = await service.get_stored_export_file(
        conn, user_id=current_user["id"], list_id=list_id, fmt=format
    )
    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if format == "xlsx"
        else "text/csv; charset=utf-8"
    )
    filename = f"panoramapj_leads_{str(list_id)[:8]}.{format}"
    return Response(
        content=file_bytes,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

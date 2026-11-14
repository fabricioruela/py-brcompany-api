"""Service logic for Export calculation, atomic FIFO credit deduction, and 30-day physical file storage."""

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import List, Tuple
from uuid import UUID, uuid4
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException, status

from brcompany_api.config import get_settings
from brcompany_api.domains.admin.router import get_export_deduplication_enabled
from brcompany_api.domains.auth.service import deduct_credits_fifo, sync_wallet_balance
from brcompany_api.domains.export.schemas import (
    ExportCalculateRequest,
    ExportCalculateResponse,
    ExportDownloadRequest,
)
from brcompany_api.domains.search.service import (
    FALLBACK_SOURCE_SQL,
    is_search_index_populated,
    resolve_effective_where_clause,
)
from brcompany_api.utils.spreadsheet import generate_csv_bytes, generate_xlsx_bytes

logger = logging.getLogger(__name__)


def _ensure_storage_dir() -> Path:
    settings = get_settings()
    storage_dir = Path(settings.app_export_storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir


async def cleanup_expired_export_files(conn: psycopg.AsyncConnection) -> None:
    """Remove physical CSV/XLSX files from disk when their 30-day retention window has passed."""
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT id, csv_file_path, xlsx_file_path
                FROM app_saas.export_lists
                WHERE expires_at <= clock_timestamp()
                  AND (csv_file_path IS NOT NULL OR xlsx_file_path IS NOT NULL)
                LIMIT 50;
                """
            )
            expired_rows = await cur.fetchall()
            for r in expired_rows:
                for path_key in ("csv_file_path", "xlsx_file_path"):
                    fpath = r.get(path_key)
                    if fpath:
                        try:
                            Path(fpath).unlink(missing_ok=True)
                        except Exception:
                            pass
                await cur.execute(
                    """
                    UPDATE app_saas.export_lists
                    SET csv_file_path = NULL, xlsx_file_path = NULL
                    WHERE id = %(id)s;
                    """,
                    {"id": r["id"]},
                )
        await conn.commit()
    except Exception as exc:
        logger.debug("Cleanup of expired export files skipped: %s", exc)


async def calculate_export_cost(
    conn: psycopg.AsyncConnection,
    user_id: UUID,
    req: ExportCalculateRequest,
) -> ExportCalculateResponse:
    """Calculate total matching companies, already exported count, and net credits required."""
    settings = get_settings()
    max_rows = settings.app_max_export_rows
    desired = min(max(req.desired_quantity, 1), max_rows)

    dedup_enabled = await get_export_deduplication_enabled(conn)
    user_balance = await sync_wallet_balance(conn, user_id)

    use_index = await is_search_index_populated(conn)
    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = await resolve_effective_where_clause(
        conn, req.filters, alias="csi", use_index_table=use_index
    )
    if where_sql == "FALSE":
        return ExportCalculateResponse(
            total_matching=0,
            already_exported_in_search=0,
            desired_quantity=desired,
            exportable_quantity=0,
            already_exported_included_free=0,
            net_credits_required=0,
            user_balance=user_balance,
            has_sufficient_balance=True,
            deduplication_enabled=dedup_enabled,
            max_export_rows=max_rows,
        )
    params["user_id"] = user_id
    params["desired"] = desired

    async with conn.cursor(row_factory=dict_row) as cur:
        # Total matching and total already exported by user within this filter
        await cur.execute(
            f"""
            SELECT
                COUNT(*)::int AS total_matching,
                COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM app_saas.user_exported_cnpjs uec
                        WHERE uec.user_id = %(user_id)s AND uec.cnpj = csi.cnpj
                    )
                )::int AS already_exported_in_search
            FROM {source_table} csi
            WHERE {where_sql};
            """,
            params,
        )
        summary = await cur.fetchone() or {"total_matching": 0, "already_exported_in_search": 0}
        total_matching = int(summary["total_matching"])
        already_exported_in_search = int(summary["already_exported_in_search"])

        if total_matching == 0:
            return ExportCalculateResponse(
                total_matching=0,
                already_exported_in_search=0,
                desired_quantity=desired,
                exportable_quantity=0,
                already_exported_included_free=0,
                net_credits_required=0,
                user_balance=user_balance,
                has_sufficient_balance=True,
                deduplication_enabled=dedup_enabled,
                max_export_rows=max_rows,
            )

        if dedup_enabled and req.skip_already_exported:
            # Only export net-new companies not yet exported by this user
            net_new_available = max(0, total_matching - already_exported_in_search)
            exportable_qty = min(desired, net_new_available)
            already_included_free = 0
            net_credits = exportable_qty
        elif dedup_enabled and not req.skip_already_exported:
            # Export top N matching companies, charging 0 for those already exported
            await cur.execute(
                f"""
                WITH batch AS (
                    SELECT
                        csi.cnpj,
                        EXISTS (
                            SELECT 1 FROM app_saas.user_exported_cnpjs uec
                            WHERE uec.user_id = %(user_id)s AND uec.cnpj = csi.cnpj
                        ) AS is_already_exported
                    FROM {source_table} csi
                    WHERE {where_sql}
                    ORDER BY csi.start_activity_date DESC NULLS LAST, csi.cnpj ASC
                    LIMIT %(desired)s
                )
                SELECT
                    COUNT(*)::int AS batch_total,
                    COUNT(*) FILTER (WHERE is_already_exported)::int AS batch_already_exported
                FROM batch;
                """,
                params,
            )
            b_row = await cur.fetchone() or {"batch_total": 0, "batch_already_exported": 0}
            exportable_qty = int(b_row["batch_total"])
            already_included_free = int(b_row["batch_already_exported"])
            net_credits = max(0, exportable_qty - already_included_free)
        else:
            # Platform Administrator disabled deduplication: charge 1 credit per exported row
            exportable_qty = min(desired, total_matching)
            already_included_free = 0
            net_credits = exportable_qty

    return ExportCalculateResponse(
        total_matching=total_matching,
        already_exported_in_search=already_exported_in_search if dedup_enabled else 0,
        desired_quantity=desired,
        exportable_quantity=exportable_qty,
        already_exported_included_free=already_included_free,
        net_credits_required=net_credits,
        user_balance=user_balance,
        has_sufficient_balance=(user_balance >= net_credits),
        deduplication_enabled=dedup_enabled,
        max_export_rows=max_rows,
    )


async def execute_export_and_store_files(
    conn: psycopg.AsyncConnection,
    user_id: UUID,
    req: ExportDownloadRequest,
) -> Tuple[UUID, str, bytes]:
    """Execute atomic export, deduct FIFO credits, save 30-day CSV/XLSX files, and return requested file bytes."""
    await cleanup_expired_export_files(conn)

    settings = get_settings()
    max_rows = settings.app_max_export_rows
    desired = min(max(req.desired_quantity, 1), max_rows)
    dedup_enabled = await get_export_deduplication_enabled(conn)

    use_index = await is_search_index_populated(conn)
    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = await resolve_effective_where_clause(
        conn, req.filters, alias="csi", use_index_table=use_index
    )
    if where_sql == "FALSE":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nenhuma empresa encontrada na área geográfica selecionada.",
        )
    params["user_id"] = user_id
    params["desired"] = desired

    if dedup_enabled and req.skip_already_exported:
        where_sql = (
            f"{where_sql} AND NOT EXISTS ("
            f"SELECT 1 FROM app_saas.user_exported_cnpjs uec "
            f"WHERE uec.user_id = %(user_id)s AND uec.cnpj = csi.cnpj)"
        )

    query = f"""
        SELECT
            csi.cnpj,
            csi.cnpj_base,
            csi.identifier_type,
            csi.legal_name,
            csi.trade_name,
            csi.start_activity_date,
            csi.main_cnae,
            csi.main_cnae_description,
            csi.secondary_cnaes,
            csi.legal_nature_code,
            csi.legal_nature_description,
            csi.company_size,
            csi.share_capital,
            csi.simples_optant,
            csi.mei_optant,
            csi.state,
            csi.municipality_code,
            csi.municipality_name,
            csi.neighborhood,
            csi.postal_code,
            csi.street_type,
            csi.street,
            csi.number,
            csi.complement,
            csi.area_code_1,
            csi.phone_1,
            csi.area_code_2,
            csi.phone_2,
            csi.email,
            csi.partners_summary,
            EXISTS (
                SELECT 1 FROM app_saas.user_exported_cnpjs uec
                WHERE uec.user_id = %(user_id)s AND uec.cnpj = csi.cnpj
            ) AS is_already_exported
        FROM {source_table} csi
        WHERE {where_sql}
        ORDER BY csi.start_activity_date DESC NULLS LAST, csi.cnpj ASC
        LIMIT %(desired)s;
    """

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nenhuma empresa encontrada para os critérios selecionados (ou todas já foram exportadas anteriormente).",
        )

    # Enrich partners_summary on the fly if fallback source was used
    if not use_index:
        base_cnpjs = list({r["cnpj_base"] for r in rows if r.get("cnpj_base")})
        if base_cnpjs:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        p.cnpj_base,
                        string_agg(
                            CASE
                                WHEN pq.description IS NOT NULL THEN p.partner_name || ' (' || pq.description || ')'
                                ELSE p.partner_name
                            END,
                            '; ' ORDER BY p.id
                        ) AS partners_summary
                    FROM partner p
                    LEFT JOIN partner_qualification pq ON pq.code = p.qualification_code
                    WHERE p.cnpj_base = ANY(%(bases)s)
                      AND p.partner_name IS NOT NULL
                    GROUP BY p.cnpj_base;
                    """,
                    {"bases": base_cnpjs},
                )
                partner_map = {pr["cnpj_base"]: pr["partners_summary"] for pr in await cur.fetchall()}
                for r in rows:
                    r["partners_summary"] = partner_map.get(r["cnpj_base"], "")

    if dedup_enabled:
        net_credits = sum(1 for r in rows if not r.get("is_already_exported"))
    else:
        net_credits = len(rows)

    list_id = uuid4()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    list_name = (req.list_name or "").strip() or f"Lista B2B ({len(rows)} empresas) - {now_str}"
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.app_export_retention_days)

    # Generate both CSV and XLSX physical files for 30-day retention
    csv_bytes = generate_csv_bytes(rows)
    xlsx_bytes = generate_xlsx_bytes(rows)

    storage_dir = _ensure_storage_dir()
    csv_path = storage_dir / f"{list_id}.csv"
    xlsx_path = storage_dir / f"{list_id}.xlsx"
    csv_path.write_bytes(csv_bytes)
    xlsx_path.write_bytes(xlsx_bytes)

    try:
        # 1. Record export_lists entry first (so credit_transactions FK can reference it)
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO app_saas.export_lists (
                    id, user_id, name, filters_json, total_records, credits_charged,
                    deduplicated, csv_file_path, xlsx_file_path, expires_at
                ) VALUES (
                    %(id)s, %(user_id)s, %(name)s, %(filters_json)s::jsonb, %(total_records)s,
                    %(credits_charged)s, %(deduplicated)s, %(csv_path)s, %(xlsx_path)s, %(expires_at)s
                );
                """,
                {
                    "id": list_id,
                    "user_id": user_id,
                    "name": list_name,
                    "filters_json": json.dumps(req.filters.model_dump(mode="json")),
                    "total_records": len(rows),
                    "credits_charged": net_credits,
                    "deduplicated": dedup_enabled,
                    "csv_path": str(csv_path),
                    "xlsx_path": str(xlsx_path),
                    "expires_at": expires_at,
                },
            )

        # 2. Deduct credits FIFO atomically
        await deduct_credits_fifo(
            conn,
            user_id=user_id,
            amount_to_deduct=net_credits,
            description=f"Exportação: {list_name} ({len(rows)} leads, {net_credits} créditos)",
            export_list_id=list_id,
        )

        # 3. Record exported CNPJs in user_exported_cnpjs
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO app_saas.user_exported_cnpjs (user_id, cnpj, first_export_list_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, cnpj) DO NOTHING;
                """,
                [(user_id, r["cnpj"], list_id) for r in rows],
            )

        await conn.commit()
    except Exception:
        await conn.rollback()
        csv_path.unlink(missing_ok=True)
        xlsx_path.unlink(missing_ok=True)
        raise

    payload_bytes = xlsx_bytes if req.format == "xlsx" else csv_bytes
    return list_id, list_name, payload_bytes


async def list_user_export_history(
    conn: psycopg.AsyncConnection, user_id: UUID
) -> List[dict]:
    """List exported lists for the given user ordered by most recent."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                id, name, filters_json, total_records, credits_charged,
                deduplicated, expires_at, created_at
            FROM app_saas.export_lists
            WHERE user_id = %(user_id)s
            ORDER BY created_at DESC
            LIMIT 100;
            """,
            {"user_id": user_id},
        )
        return await cur.fetchall()


async def get_stored_export_file(
    conn: psycopg.AsyncConnection,
    user_id: UUID,
    list_id: UUID,
    fmt: str = "csv",
) -> Tuple[str, bytes]:
    """Retrieve a previously generated CSV or XLSX file within its 30-day retention period."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT id, name, csv_file_path, xlsx_file_path, expires_at
            FROM app_saas.export_lists
            WHERE id = %(list_id)s AND user_id = %(user_id)s
            LIMIT 1;
            """,
            {"list_id": list_id, "user_id": user_id},
        )
        row = await cur.fetchone()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lista de exportação não encontrada.",
        )

    now = datetime.now(timezone.utc)
    if row["expires_at"] <= now:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="O prazo de retenção de 30 dias deste arquivo expirou.",
        )

    file_path_str = row["xlsx_file_path"] if fmt == "xlsx" else row["csv_file_path"]
    if not file_path_str or not Path(file_path_str).is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Arquivo físico não encontrado no servidor de armazenamento.",
        )

    return row["name"], Path(file_path_str).read_bytes()

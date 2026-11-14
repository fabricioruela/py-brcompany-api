"""Database queries, cache, and filter builder for the PanoramaPJ Search Engine."""

from collections import OrderedDict
import json
import time
from typing import Any, Dict, List, Optional, Tuple
from fastapi import HTTPException, status
import psycopg
from psycopg.rows import dict_row

from brcompany_api.domains.geo import service as geo_service
from brcompany_api.domains.search.schemas import (
    MapCenterPoint,
    MapClusterItem,
    MapCompanyItem,
    MaskedCompanyPreviewItem,
    SearchFilterRequest,
)
from brcompany_api.utils.masking import mask_email, mask_phone, mask_street

MAP_INDIVIDUAL_PIN_THRESHOLD = 5000

_INDEX_POPULATED_TTL_SECONDS = 60.0
_index_populated_cache: Optional[Tuple[bool, float]] = None
_search_count_cache: "OrderedDict[Tuple[str, str], Tuple[int, float]]" = OrderedDict()
_search_preview_cache: "OrderedDict[Tuple[str, str, int, int], Tuple[Tuple[int, List[MaskedCompanyPreviewItem]], float]]" = OrderedDict()
_search_map_cache: "OrderedDict[Tuple[str, str, int, int], Tuple[Tuple[int, str, List[MapCompanyItem], List[MapClusterItem], bool, Optional[int], Optional[MapCenterPoint]], float]]" = OrderedDict()


def invalidate_search_cache() -> None:
    """Clear in-memory search caches and index population cache."""
    global _index_populated_cache
    _index_populated_cache = None
    _search_count_cache.clear()
    _search_preview_cache.clear()
    _search_map_cache.clear()


def _clean_states(filters: SearchFilterRequest) -> List[str]:
    return [s.strip().upper()[:2] for s in filters.states if s.strip()]


def _clean_muns(filters: SearchFilterRequest) -> List[str]:
    return [m.strip()[:4] for m in filters.municipality_codes if m.strip()]


def validate_required_location_filters(filters: SearchFilterRequest) -> None:
    """Require at least one UF and one municipality code before querying."""
    if not _clean_states(filters) or not _clean_muns(filters):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Estado (UF) e Município são obrigatórios para esta operação.",
        )


def _has_other_filters(filters: SearchFilterRequest) -> bool:
    return bool(
        (filters.q and filters.q.strip())
        or any(n.strip() for n in filters.neighborhoods)
        or any(c.strip() for c in filters.main_cnaes)
        or any(c.strip() for c in filters.secondary_cnaes)
        or any(s.strip() for s in filters.company_sizes)
        or any(n.strip() for n in filters.legal_nature_codes)
        or filters.identifier_type in ("1", "2")
        or filters.simples_optant is not None
        or filters.mei_optant is not None
        or filters.start_date_from is not None
        or filters.start_date_to is not None
        or filters.min_share_capital is not None
        or filters.max_share_capital is not None
        or filters.only_with_phone
        or filters.only_with_email
        or filters.geo_radius is not None
        or filters.geo_bounds is not None
    )


def is_unfiltered_search(filters: SearchFilterRequest) -> bool:
    """Return True when no effective filter constraints are active."""
    return not _clean_states(filters) and not _clean_muns(filters) and not _has_other_filters(filters)


def is_location_only_search(filters: SearchFilterRequest) -> bool:
    """Return True when filtering exclusively by UF and optionally municipality codes."""
    return bool(_clean_states(filters)) and not _has_other_filters(filters)


def _normalized_filter_key(filters: SearchFilterRequest) -> str:
    payload = filters.model_dump(mode="json")
    payload["q"] = (payload.get("q") or "").strip()
    payload["states"] = sorted(set(_clean_states(filters)))
    payload["municipality_codes"] = sorted(set(_clean_muns(filters)))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


async def get_search_cache_epoch(conn: psycopg.AsyncConnection) -> str:
    """Return an epoch token combining latest published ETL run and active LGPD opt-outs."""
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    COALESCE((SELECT MAX(id) FROM etl.load_run WHERE status = 'published'), 0) AS etl_run_id,
                    (SELECT COUNT(*) FROM app_saas.company_opt_outs WHERE status = 'active') AS opt_out_count,
                    (SELECT MAX(updated_at) FROM app_saas.company_opt_outs) AS opt_out_ts;
                """
            )
            row = await cur.fetchone()
        if row:
            return f"{row.get('etl_run_id', 0)}:{row.get('opt_out_count', 0)}:{row.get('opt_out_ts') or ''}"
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
    return "0:0:"


async def is_search_index_populated(conn: psycopg.AsyncConnection) -> bool:
    """Check whether registry.company_search_index exists and has at least one row."""
    global _index_populated_cache
    now = time.monotonic()
    if (
        _index_populated_cache is not None
        and _index_populated_cache[0]
        and (now - _index_populated_cache[1]) < _INDEX_POPULATED_TTL_SECONDS
    ):
        return True

    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'company_search_index'
                ) AS has_table;
                """
            )
            row = await cur.fetchone()
            if not row or not row["has_table"]:
                return False
            await cur.execute("SELECT 1 FROM company_search_index LIMIT 1;")
            populated = (await cur.fetchone()) is not None
            if populated:
                _index_populated_cache = (True, now)
            return populated
    except Exception:
        return False


def build_where_clause(
    filters: SearchFilterRequest,
    alias: str = "csi",
    use_index_table: bool = True,
) -> Tuple[str, Dict[str, Any]]:
    """Build parameterized WHERE conditions for company_search_index (or fallback view)."""
    clauses: List[str] = []
    params: Dict[str, Any] = {}

    if not use_index_table:
        clauses.append(f"{alias}.registration_status = '02'")

    # Exclude LGPD Opt-Out records
    clauses.append(
        f"NOT EXISTS (SELECT 1 FROM app_saas.company_opt_outs oo WHERE oo.cnpj = {alias}.cnpj AND oo.status = 'active')"
    )

    if filters.q and filters.q.strip():
        q_clean = filters.q.strip()
        params["q_like"] = f"%{q_clean}%"
        if use_index_table:
            clauses.append(f"{alias}.search_text ILIKE %(q_like)s")
        else:
            clauses.append(
                f"({alias}.legal_name ILIKE %(q_like)s OR {alias}.trade_name ILIKE %(q_like)s)"
            )

    if filters.states:
        clean_states = [s.strip().upper()[:2] for s in filters.states if s.strip()]
        if clean_states:
            params["states"] = clean_states
            clauses.append(f"{alias}.state = ANY(%(states)s)")

    if filters.municipality_codes:
        clean_muns = [m.strip()[:4] for m in filters.municipality_codes if m.strip()]
        if clean_muns:
            params["municipality_codes"] = clean_muns
            clauses.append(f"{alias}.municipality_code = ANY(%(municipality_codes)s)")

    if filters.neighborhoods:
        clean_neigh = [n.strip().upper() for n in filters.neighborhoods if n.strip()]
        if clean_neigh:
            params["neighborhoods"] = clean_neigh
            clauses.append(f"UPPER({alias}.neighborhood) = ANY(%(neighborhoods)s)")

    if filters.main_cnaes:
        clean_cnaes = [
            "".join(ch for ch in c if ch.isdigit())[:7]
            for c in filters.main_cnaes
            if c.strip()
        ]
        clean_cnaes = [c for c in clean_cnaes if c]
        if clean_cnaes:
            params["main_cnaes"] = clean_cnaes
            if filters.include_secondary_cnae:
                clauses.append(
                    f"({alias}.main_cnae = ANY(%(main_cnaes)s) OR {alias}.secondary_cnaes::text[] && %(main_cnaes)s::text[])"
                )
            else:
                clauses.append(f"{alias}.main_cnae = ANY(%(main_cnaes)s)")

    if filters.secondary_cnaes:
        clean_sec = [
            "".join(ch for ch in c if ch.isdigit())[:7]
            for c in filters.secondary_cnaes
            if c.strip()
        ]
        clean_sec = [c for c in clean_sec if c]
        if clean_sec:
            params["secondary_cnaes"] = clean_sec
            clauses.append(f"{alias}.secondary_cnaes::text[] && %(secondary_cnaes)s::text[]")

    if filters.company_sizes:
        clean_sizes = [s.strip()[:2] for s in filters.company_sizes if s.strip()]
        if clean_sizes:
            params["company_sizes"] = clean_sizes
            clauses.append(f"{alias}.company_size = ANY(%(company_sizes)s)")

    if filters.legal_nature_codes:
        clean_natures = [
            "".join(ch for ch in n if ch.isdigit())[:4]
            for n in filters.legal_nature_codes
            if n.strip()
        ]
        clean_natures = [n for n in clean_natures if n]
        if clean_natures:
            params["legal_nature_codes"] = clean_natures
            clauses.append(f"{alias}.legal_nature_code = ANY(%(legal_nature_codes)s)")

    if filters.identifier_type in ("1", "2"):
        params["identifier_type"] = filters.identifier_type
        clauses.append(f"{alias}.identifier_type = %(identifier_type)s")

    if filters.simples_optant is not None:
        params["simples_optant"] = bool(filters.simples_optant)
        clauses.append(f"{alias}.simples_optant = %(simples_optant)s")

    if filters.mei_optant is not None:
        params["mei_optant"] = bool(filters.mei_optant)
        clauses.append(f"{alias}.mei_optant = %(mei_optant)s")

    if filters.start_date_from is not None:
        params["start_date_from"] = filters.start_date_from
        clauses.append(f"{alias}.start_activity_date >= %(start_date_from)s")

    if filters.start_date_to is not None:
        params["start_date_to"] = filters.start_date_to
        clauses.append(f"{alias}.start_activity_date <= %(start_date_to)s")

    if filters.min_share_capital is not None:
        params["min_share_capital"] = filters.min_share_capital
        clauses.append(f"{alias}.share_capital >= %(min_share_capital)s")

    if filters.max_share_capital is not None:
        params["max_share_capital"] = filters.max_share_capital
        clauses.append(f"{alias}.share_capital <= %(max_share_capital)s")

    if filters.only_with_phone:
        clauses.append(f"{alias}.has_phone = true")

    if filters.only_with_email:
        clauses.append(f"{alias}.has_email = true")

    where_sql = " AND ".join(clauses) if clauses else "TRUE"
    return where_sql, params


async def resolve_effective_where_clause(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    alias: str = "csi",
    use_index_table: bool = True,
    spatial_targets: Optional[dict] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Build WHERE SQL including PostGIS-resolved CEP/municipality filters when spatial bounds/radius are set."""
    where_sql, params = build_where_clause(filters, alias=alias, use_index_table=use_index_table)
    if filters.geo_radius is None and filters.geo_bounds is None:
        return where_sql, params

    spatial = (
        spatial_targets
        if spatial_targets is not None
        else await geo_service.resolve_spatial_filter_targets(
            conn,
            geo_radius=filters.geo_radius,
            geo_bounds=filters.geo_bounds,
        )
    )
    if not spatial.get("active"):
        return where_sql, params
    if spatial.get("empty"):
        return "FALSE", {}

    extra_clauses: List[str] = []
    if spatial.get("states"):
        params["geo_states"] = spatial["states"]
        extra_clauses.append(f"{alias}.state = ANY(%(geo_states)s)")
    if spatial.get("municipality_codes"):
        params["geo_muni_codes"] = spatial["municipality_codes"]
        extra_clauses.append(f"{alias}.municipality_code = ANY(%(geo_muni_codes)s)")
    if spatial.get("postal_codes"):
        params["geo_postal_codes"] = spatial["postal_codes"]
        extra_clauses.append(f"{alias}.postal_code = ANY(%(geo_postal_codes)s)")

    if not extra_clauses:
        return "FALSE", {}
    return f"({where_sql}) AND " + " AND ".join(extra_clauses), params


FALLBACK_SOURCE_SQL = """
(
    SELECT
        e.cnpj,
        e.cnpj_base,
        e.identifier_type,
        COALESCE(c.legal_name, e.trade_name, 'RAZAO SOCIAL NAO INFORMADA') AS legal_name,
        e.trade_name,
        e.registration_status,
        e.start_activity_date,
        e.main_cnae,
        cn.description AS main_cnae_description,
        e.secondary_cnaes,
        c.legal_nature_code,
        ln.description AS legal_nature_description,
        COALESCE(c.company_size, '00') AS company_size,
        c.share_capital,
        (s.simples_optant = 'S') AS simples_optant,
        (s.mei_optant = 'S') AS mei_optant,
        e.state,
        e.municipality_code,
        COALESCE(m.description, 'NAO INFORMADO') AS municipality_name,
        e.neighborhood,
        e.postal_code,
        e.street_type,
        e.street,
        e.number,
        e.complement,
        e.area_code_1,
        e.phone_1,
        e.area_code_2,
        e.phone_2,
        e.email,
        (e.phone_1 IS NOT NULL AND length(trim(e.phone_1)) >= 6) AS has_phone,
        (e.email IS NOT NULL AND position('@' in e.email) > 1) AS has_email,
        NULL::text AS partners_summary
    FROM establishment e
    LEFT JOIN company c ON c.cnpj_base = e.cnpj_base
    LEFT JOIN simples s ON s.cnpj_base = e.cnpj_base
    LEFT JOIN municipality m ON m.code = e.municipality_code
    LEFT JOIN cnae cn ON cn.code = e.main_cnae
    LEFT JOIN legal_nature ln ON ln.code = c.legal_nature_code
)
"""


async def _estimate_unfiltered_count(conn: psycopg.AsyncConnection) -> Optional[int]:
    """Estimate active company count via pg_class.reltuples minus active LGPD opt-outs."""
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT c.reltuples::bigint AS estimate
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema()
                  AND c.relname = 'company_search_index'
                LIMIT 1;
                """
            )
            row = await cur.fetchone()
            estimate = int(row["estimate"]) if row and row.get("estimate") is not None else 0
            if estimate <= 0:
                return None

            await cur.execute(
                "SELECT COUNT(*)::bigint AS total FROM app_saas.company_opt_outs WHERE status = 'active';"
            )
            opt_row = await cur.fetchone()
            opt_outs = int(opt_row["total"]) if opt_row and opt_row.get("total") is not None else 0
            return max(0, estimate - opt_outs)
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        return None


async def _estimate_location_count(
    conn: psycopg.AsyncConnection, states: List[str], municipality_codes: List[str]
) -> Optional[int]:
    """Compute count for state / state+municipality filters from pre-aggregated location_hierarchy."""
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            if municipality_codes:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(active_count), 0)::bigint AS total
                    FROM location_hierarchy
                    WHERE state = ANY(%(states)s)
                      AND municipality_code = ANY(%(municipality_codes)s);
                    """,
                    {"states": states, "municipality_codes": municipality_codes},
                )
            else:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(active_count), 0)::bigint AS total
                    FROM location_hierarchy
                    WHERE state = ANY(%(states)s);
                    """,
                    {"states": states},
                )
            row = await cur.fetchone()
            base_total = int(row["total"]) if row and row.get("total") is not None else 0
            if base_total <= 0:
                return None

            if municipality_codes:
                await cur.execute(
                    """
                    SELECT COUNT(*)::bigint AS opt_outs
                    FROM app_saas.company_opt_outs oo
                    JOIN company_search_index csi ON csi.cnpj = oo.cnpj
                    WHERE oo.status = 'active'
                      AND csi.state = ANY(%(states)s)
                      AND csi.municipality_code = ANY(%(municipality_codes)s);
                    """,
                    {"states": states, "municipality_codes": municipality_codes},
                )
            else:
                await cur.execute(
                    """
                    SELECT COUNT(*)::bigint AS opt_outs
                    FROM app_saas.company_opt_outs oo
                    JOIN company_search_index csi ON csi.cnpj = oo.cnpj
                    WHERE oo.status = 'active'
                      AND csi.state = ANY(%(states)s);
                    """,
                    {"states": states},
                )
            opt_row = await cur.fetchone()
            opt_outs = int(opt_row["opt_outs"]) if opt_row and opt_row.get("opt_outs") is not None else 0
            return max(0, base_total - opt_outs)
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
        return None


async def count_matching_companies(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    spatial_targets: Optional[dict] = None,
) -> int:
    """Count total active companies matching the provided filters."""
    use_index = await is_search_index_populated(conn)

    if use_index:
        if is_unfiltered_search(filters):
            estimated = await _estimate_unfiltered_count(conn)
            if estimated is not None:
                return estimated
        elif is_location_only_search(filters):
            loc_count = await _estimate_location_count(
                conn, _clean_states(filters), _clean_muns(filters)
            )
            if loc_count is not None:
                return loc_count

    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = await resolve_effective_where_clause(
        conn,
        filters,
        alias="csi",
        use_index_table=use_index,
        spatial_targets=spatial_targets,
    )
    if where_sql == "FALSE":
        return 0

    query = f"""
        SELECT COUNT(*)::int AS total
        FROM {source_table} csi
        WHERE {where_sql};
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
        return int(row["total"]) if row else 0


def _row_to_masked_preview_dict(r: dict) -> dict:
    return {
        "cnpj": r["cnpj"],
        "cnpj_base": r["cnpj_base"],
        "identifier_type": r.get("identifier_type"),
        "legal_name": r.get("legal_name") or "RAZAO SOCIAL NAO INFORMADA",
        "trade_name": r.get("trade_name"),
        "start_activity_date": r.get("start_activity_date"),
        "main_cnae": r.get("main_cnae"),
        "main_cnae_description": r.get("main_cnae_description"),
        "company_size": r.get("company_size"),
        "share_capital": r.get("share_capital"),
        "simples_optant": bool(r.get("simples_optant")),
        "mei_optant": bool(r.get("mei_optant")),
        "state": r.get("state"),
        "municipality_code": r.get("municipality_code"),
        "municipality_name": r.get("municipality_name"),
        "neighborhood": r.get("neighborhood"),
        "street_masked": mask_street(r.get("street_type"), r.get("street"), r.get("number")),
        "phone_masked": mask_phone(r.get("area_code_1"), r.get("phone_1")),
        "email_masked": mask_email(r.get("email")),
        "has_phone": bool(r.get("has_phone")),
        "has_email": bool(r.get("has_email")),
    }


async def preview_matching_companies(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    limit: int = 50,
    offset: int = 0,
    spatial_targets: Optional[dict] = None,
) -> List[MaskedCompanyPreviewItem]:
    """Fetch paginated matching companies with masked contact and street fields."""
    use_index = await is_search_index_populated(conn)
    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = await resolve_effective_where_clause(
        conn,
        filters,
        alias="csi",
        use_index_table=use_index,
        spatial_targets=spatial_targets,
    )
    if where_sql == "FALSE":
        return []
    params["limit"] = min(max(limit, 1), 200)
    params["offset"] = max(offset, 0)

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
            csi.company_size,
            csi.share_capital,
            csi.simples_optant,
            csi.mei_optant,
            csi.state,
            csi.municipality_code,
            csi.municipality_name,
            csi.neighborhood,
            csi.street_type,
            csi.street,
            csi.number,
            csi.area_code_1,
            csi.phone_1,
            csi.email,
            csi.has_phone,
            csi.has_email
        FROM {source_table} csi
        WHERE {where_sql}
        ORDER BY csi.start_activity_date DESC NULLS LAST, csi.cnpj ASC
        LIMIT %(limit)s OFFSET %(offset)s;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()

    return [MaskedCompanyPreviewItem(**_row_to_masked_preview_dict(r)) for r in rows]


async def search_map_companies(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    limit: int = 500,
    offset: int = 0,
    spatial_targets: Optional[dict] = None,
) -> Tuple[List[MapCompanyItem], int, Optional[MapCenterPoint]]:
    """Fetch paginated matching companies with masked contacts and br_company.geo coordinates."""
    safe_limit = min(max(limit, 1), 2000)
    safe_offset = max(offset, 0)
    use_index = await is_search_index_populated(conn)
    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = await resolve_effective_where_clause(
        conn,
        filters,
        alias="csi",
        use_index_table=use_index,
        spatial_targets=spatial_targets,
    )
    if where_sql == "FALSE":
        return [], 0, _compute_filter_center(filters, [])
    params["limit"] = safe_limit
    params["offset"] = safe_offset

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
            csi.area_code_1,
            csi.phone_1,
            csi.email,
            csi.has_phone,
            csi.has_email
        FROM {source_table} csi
        WHERE {where_sql}
        ORDER BY csi.start_activity_date DESC NULLS LAST, csi.cnpj ASC
        LIMIT %(limit)s OFFSET %(offset)s;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()

    geo_by_cnpj = await geo_service.resolve_establishments_geo_batch(
        conn, rows, mask_mei_exact=True
    )
    items: List[MapCompanyItem] = []
    for r in rows:
        coord = geo_by_cnpj.get(r["cnpj"])
        if not coord:
            continue
        items.append(
            MapCompanyItem(
                **_row_to_masked_preview_dict(r),
                latitude=coord["latitude"],
                longitude=coord["longitude"],
                geo_precision=coord["precision"],
            )
        )

    center = _compute_filter_center(filters, items)
    return items, len(rows), center


async def search_map_clusters(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    total_companies: int,
    spatial_targets: Optional[dict] = None,
) -> Tuple[List[MapClusterItem], Optional[MapCenterPoint]]:
    """Aggregate massive result sets into server-side geographic clusters by CEP prefix."""
    use_index = await is_search_index_populated(conn)
    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = await resolve_effective_where_clause(
        conn,
        filters,
        alias="csi",
        use_index_table=use_index,
        spatial_targets=spatial_targets,
    )
    if where_sql == "FALSE":
        return [], _compute_filter_center(filters, [])

    prefix_len = 4 if total_companies > 15000 else 5
    params["prefix_len"] = prefix_len

    query = f"""
        SELECT
            csi.state,
            LEFT(
                COALESCE(NULLIF(regexp_replace(COALESCE(csi.postal_code, ''), '\\D', '', 'g'), ''), ''),
                %(prefix_len)s
            ) AS cep_prefix,
            MIN(
                CASE
                    WHEN length(regexp_replace(COALESCE(csi.postal_code, ''), '\\D', '', 'g')) = 8
                         AND regexp_replace(COALESCE(csi.postal_code, ''), '\\D', '', 'g') <> '00000000'
                    THEN regexp_replace(COALESCE(csi.postal_code, ''), '\\D', '', 'g')
                    ELSE NULL
                END
            ) AS sample_cep,
            MAX(csi.municipality_name) AS municipality_name,
            MAX(NULLIF(TRIM(csi.neighborhood), '')) AS neighborhood,
            COUNT(*)::int AS company_count
        FROM {source_table} csi
        WHERE {where_sql}
        GROUP BY 1, 2
        ORDER BY company_count DESC;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()

    cep_pairs: List[Tuple[str, str]] = []
    city_pairs: List[Tuple[str, str]] = []
    for r in rows:
        uf = geo_service.clean_state(r.get("state"))
        sample_cep = geo_service.clean_postal_code(r.get("sample_cep"))
        city = (r.get("municipality_name") or "").strip()
        if uf and sample_cep:
            cep_pairs.append((uf, sample_cep))
        if uf and city:
            city_pairs.append((uf, city))

    cep_centroids, city_centroids = await geo_service.resolve_centroids_for_groups(
        conn, cep_pairs, city_pairs
    )

    merged: Dict[Tuple[float, float], Dict[str, Any]] = {}
    for r in rows:
        uf = geo_service.clean_state(r.get("state"))
        sample_cep = geo_service.clean_postal_code(r.get("sample_cep"))
        city = (r.get("municipality_name") or "").strip()
        coord = None
        if uf and sample_cep and (uf, sample_cep) in cep_centroids:
            coord = cep_centroids[(uf, sample_cep)]
        elif uf and city and (uf, city) in city_centroids:
            coord = city_centroids[(uf, city)]
        if not coord:
            continue

        lat, lon = coord
        coord_key = (round(lat, 5), round(lon, 5))
        cnt = int(r.get("company_count") or 0)
        if cnt <= 0:
            continue
        cep_prefix = (r.get("cep_prefix") or "").strip() or None
        neighborhood = (r.get("neighborhood") or "").strip() or None
        if coord_key in merged:
            merged[coord_key]["count"] += cnt
            if not merged[coord_key]["neighborhood"] and neighborhood:
                merged[coord_key]["neighborhood"] = neighborhood
        else:
            label_parts = []
            if neighborhood:
                label_parts.append(neighborhood)
            if cep_prefix:
                label_parts.append(f"CEP {cep_prefix}*")
            if not label_parts:
                label_parts.append(city or uf or "Região")
            merged[coord_key] = {
                "cluster_key": f"{uf or 'BR'}:{cep_prefix or coord_key[0]}:{coord_key[1]}",
                "label": " • ".join(label_parts),
                "latitude": lat,
                "longitude": lon,
                "count": cnt,
                "postal_code_prefix": cep_prefix,
                "neighborhood": neighborhood,
                "municipality_name": city or None,
                "state": uf,
            }

    clusters = [
        MapClusterItem(**entry)
        for entry in sorted(merged.values(), key=lambda x: x["count"], reverse=True)
    ]

    if filters.geo_radius is not None:
        center = MapCenterPoint(
            latitude=filters.geo_radius.latitude,
            longitude=filters.geo_radius.longitude,
        )
    elif filters.geo_bounds is not None:
        center = MapCenterPoint(
            latitude=(filters.geo_bounds.north + filters.geo_bounds.south) / 2.0,
            longitude=(filters.geo_bounds.east + filters.geo_bounds.west) / 2.0,
        )
    elif clusters:
        total_weight = sum(c.count for c in clusters) or 1
        center = MapCenterPoint(
            latitude=sum(c.latitude * c.count for c in clusters) / total_weight,
            longitude=sum(c.longitude * c.count for c in clusters) / total_weight,
        )
    else:
        center = None

    return clusters, center


def _compute_filter_center(
    filters: SearchFilterRequest, items: List[MapCompanyItem]
) -> Optional[MapCenterPoint]:
    if filters.geo_radius is not None:
        return MapCenterPoint(
            latitude=filters.geo_radius.latitude,
            longitude=filters.geo_radius.longitude,
        )
    if filters.geo_bounds is not None:
        return MapCenterPoint(
            latitude=(filters.geo_bounds.north + filters.geo_bounds.south) / 2.0,
            longitude=(filters.geo_bounds.east + filters.geo_bounds.west) / 2.0,
        )
    if not items:
        return None
    avg_lat = sum(i.latitude for i in items) / len(items)
    avg_lon = sum(i.longitude for i in items) / len(items)
    return MapCenterPoint(latitude=avg_lat, longitude=avg_lon)


def _cache_get(cache: OrderedDict, key: tuple, ttl_seconds: int):
    if ttl_seconds <= 0 or key not in cache:
        return None
    value, created_at = cache[key]
    if (time.monotonic() - created_at) >= ttl_seconds:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return value


def _cache_set(cache: OrderedDict, key: tuple, value: Any, max_entries: int) -> None:
    if max_entries <= 0:
        return
    cache[key] = (value, time.monotonic())
    cache.move_to_end(key)
    while len(cache) > max_entries:
        cache.popitem(last=False)


async def get_cached_count(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    ttl_seconds: int = 300,
    max_entries: int = 128,
) -> int:
    """Return total matching companies using an ETL/opt-out-aware TTL cache."""
    if ttl_seconds <= 0 or max_entries <= 0:
        return await count_matching_companies(conn, filters)

    epoch = await get_search_cache_epoch(conn)
    key = (epoch, _normalized_filter_key(filters))
    cached = _cache_get(_search_count_cache, key, ttl_seconds)
    if cached is not None:
        return cached

    total = await count_matching_companies(conn, filters)
    _cache_set(_search_count_cache, key, total, max_entries)
    return total


async def get_cached_preview(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    page: int = 1,
    page_size: int = 50,
    limit: Optional[int] = None,
    ttl_seconds: int = 300,
    max_entries: int = 128,
) -> Tuple[int, List[MaskedCompanyPreviewItem]]:
    """Return total count and paginated masked preview items using an ETL/opt-out-aware TTL cache."""
    safe_page = max(page, 1)
    safe_size = min(max(limit if limit is not None else page_size, 1), 200)
    offset = (safe_page - 1) * safe_size

    if ttl_seconds <= 0 or max_entries <= 0:
        total = await count_matching_companies(conn, filters)
        items = (
            await preview_matching_companies(conn, filters, limit=safe_size, offset=offset)
            if total > 0 and offset < total
            else []
        )
        return total, items

    epoch = await get_search_cache_epoch(conn)
    filter_key = _normalized_filter_key(filters)
    preview_key = (epoch, filter_key, safe_page, safe_size)
    cached = _cache_get(_search_preview_cache, preview_key, ttl_seconds)
    if cached is not None:
        return cached

    total = await get_cached_count(
        conn, filters, ttl_seconds=ttl_seconds, max_entries=max_entries
    )
    items = (
        await preview_matching_companies(conn, filters, limit=safe_size, offset=offset)
        if total > 0 and offset < total
        else []
    )
    result = (total, items)
    _cache_set(_search_preview_cache, preview_key, result, max_entries)
    return result


async def get_cached_map_preview(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    limit: int = 500,
    offset: int = 0,
    cluster_threshold: int = MAP_INDIVIDUAL_PIN_THRESHOLD,
    ttl_seconds: int = 300,
    max_entries: int = 128,
) -> Tuple[
    int,
    str,
    List[MapCompanyItem],
    List[MapClusterItem],
    bool,
    Optional[int],
    Optional[MapCenterPoint],
]:
    """Return total count and either progressive pins or server-side aggregated clusters."""
    safe_limit = min(max(limit, 1), 2000)
    safe_offset = max(offset, 0)

    async def _execute_uncached() -> Tuple[
        int,
        str,
        List[MapCompanyItem],
        List[MapClusterItem],
        bool,
        Optional[int],
        Optional[MapCenterPoint],
    ]:
        spatial = None
        if filters.geo_radius is not None or filters.geo_bounds is not None:
            spatial = await geo_service.resolve_spatial_filter_targets(
                conn,
                geo_radius=filters.geo_radius,
                geo_bounds=filters.geo_bounds,
            )
            if spatial.get("empty"):
                return 0, "pins", [], [], False, None, _compute_filter_center(filters, [])

        total = await count_matching_companies(conn, filters, spatial_targets=spatial)
        if total <= 0:
            return 0, "pins", [], [], False, None, _compute_filter_center(filters, [])

        if total > cluster_threshold:
            clusters, center = await search_map_clusters(
                conn,
                filters,
                total_companies=total,
                spatial_targets=spatial,
            )
            cluster_total = sum(c.count for c in clusters)
            return max(total, cluster_total), "clusters", [], clusters, False, None, center

        items, raw_count, center = await search_map_companies(
            conn,
            filters,
            limit=safe_limit,
            offset=safe_offset,
            spatial_targets=spatial,
        )
        next_off = safe_offset + raw_count
        has_more = raw_count >= safe_limit and next_off < total
        return (
            max(total, len(items)),
            "pins",
            items,
            [],
            has_more,
            next_off if has_more else None,
            center,
        )

    if ttl_seconds <= 0 or max_entries <= 0:
        return await _execute_uncached()

    epoch = await get_search_cache_epoch(conn)
    filter_key = _normalized_filter_key(filters)
    map_key = (epoch, filter_key, safe_offset, safe_limit)
    cached = _cache_get(_search_map_cache, map_key, ttl_seconds)
    if cached is not None:
        return cached

    result = await _execute_uncached()
    _cache_set(_search_map_cache, map_key, result, max_entries)
    return result

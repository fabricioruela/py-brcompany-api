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
    GeoBoundsFilter,
    MapCenterPoint,
    MapClusterItem,
    MapCompanyItem,
    MapCoordinateGroup,
    MaskedCompanyPreviewItem,
    SearchFilterRequest,
)
from brcompany_api.utils.masking import mask_email, mask_phone, mask_street

MAP_INDIVIDUAL_PIN_THRESHOLD = 5000
MAP_HIGH_ZOOM_PIN_THRESHOLD = 2000

_INDEX_POPULATED_TTL_SECONDS = 60.0
_index_populated_cache: Optional[Tuple[bool, float]] = None
_search_count_cache: "OrderedDict[Tuple[str, str], Tuple[int, float]]" = OrderedDict()
_search_preview_cache: "OrderedDict[Tuple[str, str, int, int], Tuple[Tuple[int, List[MaskedCompanyPreviewItem]], float]]" = OrderedDict()
_search_map_cache: "OrderedDict[Tuple[Any, ...], Tuple[Tuple[int, str, List[MapCompanyItem], List[MapClusterItem], List[MapCoordinateGroup], bool, Optional[int], Optional[MapCenterPoint]], float]]" = OrderedDict()


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
            clauses.append(f"{alias}.state = ANY(%(states)s::bpchar[])")

    if filters.municipality_codes:
        clean_muns = [m.strip()[:4] for m in filters.municipality_codes if m.strip()]
        if clean_muns:
            params["municipality_codes"] = clean_muns
            clauses.append(f"{alias}.municipality_code = ANY(%(municipality_codes)s::bpchar[])")

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
                    f"({alias}.main_cnae = ANY(%(main_cnaes)s::bpchar[]) OR {alias}.secondary_cnaes::text[] && %(main_cnaes)s::text[])"
                )
            else:
                clauses.append(f"{alias}.main_cnae = ANY(%(main_cnaes)s::bpchar[])")

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
            clauses.append(f"{alias}.company_size = ANY(%(company_sizes)s::bpchar[])")

    if filters.legal_nature_codes:
        clean_natures = [
            "".join(ch for ch in n if ch.isdigit())[:4]
            for n in filters.legal_nature_codes
            if n.strip()
        ]
        clean_natures = [n for n in clean_natures if n]
        if clean_natures:
            params["legal_nature_codes"] = clean_natures
            clauses.append(f"{alias}.legal_nature_code = ANY(%(legal_nature_codes)s::bpchar[])")

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
    """Build WHERE SQL including PostGIS-resolved CEP/municipality filters when spatial bounds/radius or viewport are set."""
    where_sql, params = build_where_clause(filters, alias=alias, use_index_table=use_index_table)
    if spatial_targets is None and filters.geo_radius is None and filters.geo_bounds is None:
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
        extra_clauses.append(f"{alias}.state = ANY(%(geo_states)s::bpchar[])")
    if spatial.get("municipality_codes"):
        params["geo_muni_codes"] = spatial["municipality_codes"]
        extra_clauses.append(f"{alias}.municipality_code = ANY(%(geo_muni_codes)s::bpchar[])")
    if spatial.get("postal_codes"):
        params["geo_postal_codes"] = spatial["postal_codes"]
        extra_clauses.append(f"{alias}.postal_code = ANY(%(geo_postal_codes)s::bpchar[])")

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
                    WHERE state = ANY(%(states)s::bpchar[])
                      AND municipality_code = ANY(%(municipality_codes)s::bpchar[]);
                    """,
                    {"states": states, "municipality_codes": municipality_codes},
                )
            else:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(active_count), 0)::bigint AS total
                    FROM location_hierarchy
                    WHERE state = ANY(%(states)s::bpchar[]);
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
                      AND csi.state = ANY(%(states)s::bpchar[])
                      AND csi.municipality_code = ANY(%(municipality_codes)s::bpchar[]);
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
                      AND csi.state = ANY(%(states)s::bpchar[]);
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
    has_active_spatial = bool(spatial_targets and spatial_targets.get("active"))

    if use_index and not has_active_spatial:
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

    cnpjs = [str(r["cnpj"]).strip() for r in rows if r.get("cnpj")]
    cached_geo = await geo_service._lookup_company_cache(conn, cnpjs)

    items: List[MaskedCompanyPreviewItem] = []
    for r in rows:
        item_dict = _row_to_masked_preview_dict(r)
        g = cached_geo.get(str(r["cnpj"]).strip())
        if g:
            item_dict["geo_precision"] = g["precision"]
            item_dict["geo_source"] = g.get("source")
        items.append(MaskedCompanyPreviewItem(**item_dict))

    return items


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
                geo_source=coord.get("source"),
            )
        )

    center = _compute_filter_center(filters, items)
    return items, len(rows), center


def _cep_prefix_len_for_zoom(zoom: Optional[int], total_companies: int) -> int:
    """Select CEP prefix granularity based on map zoom level (or total fallback when zoom is omitted)."""
    if zoom is None:
        return 4 if total_companies > 15000 else 5
    if zoom <= 8:
        return 3
    if zoom <= 11:
        return 4
    if zoom <= 14:
        return 5
    if zoom == 15:
        return 6
    return 8


def _bounds_intersect(
    a: GeoBoundsFilter, b: GeoBoundsFilter, *, margin_ratio: float = 0.08
) -> bool:
    """Return True when bounding box `a` overlaps `b` (expanded by margin_ratio)."""
    lat_pad = max(abs(b.north - b.south) * margin_ratio, 0.002)
    lon_pad = max(abs(b.east - b.west) * margin_ratio, 0.002)
    return not (
        a.north < (min(b.south, b.north) - lat_pad)
        or a.south > (max(b.south, b.north) + lat_pad)
        or a.east < (min(b.west, b.east) - lon_pad)
        or a.west > (max(b.west, b.east) + lon_pad)
    )


def _point_in_bounds(
    lat: float, lon: float, bounds: GeoBoundsFilter, *, margin_ratio: float = 0.08
) -> bool:
    lat_pad = max(abs(bounds.north - bounds.south) * margin_ratio, 0.0008)
    lon_pad = max(abs(bounds.east - bounds.west) * margin_ratio, 0.0008)
    return (
        (min(bounds.south, bounds.north) - lat_pad) <= lat <= (max(bounds.south, bounds.north) + lat_pad)
        and (min(bounds.west, bounds.east) - lon_pad) <= lon <= (max(bounds.west, bounds.east) + lon_pad)
    )


def _is_wide_viewport(zoom: Optional[int], viewport_bounds: Optional[GeoBoundsFilter]) -> bool:
    if viewport_bounds is None:
        return False
    if zoom is not None and zoom <= 11:
        return True
    lat_span = abs(viewport_bounds.north - viewport_bounds.south)
    lon_span = abs(viewport_bounds.east - viewport_bounds.west)
    return lat_span > 0.35 or lon_span > 0.35


async def search_map_clusters(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    total_companies: int,
    spatial_targets: Optional[dict] = None,
    *,
    zoom: Optional[int] = None,
    viewport_bounds: Optional[GeoBoundsFilter] = None,
    filter_by_viewport: bool = False,
) -> Tuple[List[MapClusterItem], Optional[MapCenterPoint]]:
    """Aggregate massive result sets into zoom-aware server-side geographic clusters with bounds."""
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

    prefix_len = _cep_prefix_len_for_zoom(zoom, total_companies)
    params["prefix_len"] = prefix_len

    query = f"""
        SELECT
            csi.state,
            LEFT(COALESCE(norm.cep, ''), %(prefix_len)s) AS cep_prefix,
            MIN(
                CASE
                    WHEN length(norm.cep) = 8 AND norm.cep <> '00000000'
                    THEN norm.cep
                    ELSE NULL
                END
            ) AS min_cep,
            MAX(
                CASE
                    WHEN length(norm.cep) = 8 AND norm.cep <> '00000000'
                    THEN norm.cep
                    ELSE NULL
                END
            ) AS max_cep,
            MAX(csi.municipality_name) AS municipality_name,
            MAX(NULLIF(TRIM(csi.neighborhood), '')) AS neighborhood,
            COUNT(*)::int AS company_count
        FROM {source_table} csi
        CROSS JOIN LATERAL (
            SELECT
                CASE
                    WHEN csi.postal_code IS NOT NULL
                     AND length(csi.postal_code::text) = 8
                     AND translate(csi.postal_code::text, '0123456789', '') = ''
                    THEN csi.postal_code::text
                    ELSE NULLIF(regexp_replace(COALESCE(csi.postal_code::text, ''), '\\D', '', 'g'), '')
                END AS cep
        ) norm
        WHERE {where_sql}
        GROUP BY 1, 2
        ORDER BY company_count DESC;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()

    cep_pairs: List[Tuple[str, str]] = []
    city_pairs: List[Tuple[str, str]] = []
    prefix_pairs: List[Tuple[str, str]] = []
    for r in rows:
        uf = geo_service.clean_state(r.get("state"))
        min_cep = geo_service.clean_postal_code(r.get("min_cep"))
        max_cep = geo_service.clean_postal_code(r.get("max_cep"))
        cep_prefix = (r.get("cep_prefix") or "").strip()
        city = (r.get("municipality_name") or "").strip()
        if uf and min_cep:
            cep_pairs.append((uf, min_cep))
        if uf and max_cep and max_cep != min_cep:
            cep_pairs.append((uf, max_cep))
        if uf and cep_prefix and len(cep_prefix) < 8:
            prefix_pairs.append((uf, cep_prefix))
        if uf and city:
            city_pairs.append((uf, city))

    cep_centroids, city_centroids = await geo_service.resolve_centroids_for_groups(
        conn, cep_pairs, city_pairs
    )
    prefix_bounds_map = (
        await geo_service.lookup_cep_prefix_bounds(conn, prefix_pairs)
        if prefix_pairs
        else {}
    )

    merged: Dict[Tuple[float, float], Dict[str, Any]] = {}
    for r in rows:
        uf = geo_service.clean_state(r.get("state"))
        min_cep = geo_service.clean_postal_code(r.get("min_cep"))
        max_cep = geo_service.clean_postal_code(r.get("max_cep"))
        cep_prefix = (r.get("cep_prefix") or "").strip() or None
        city = (r.get("municipality_name") or "").strip()

        pts: List[Tuple[float, float]] = []
        p_bounds = prefix_bounds_map.get((uf, cep_prefix)) if (uf and cep_prefix) else None
        if p_bounds:
            pts.append((p_bounds["min_lat"], p_bounds["min_lon"]))
            pts.append((p_bounds["max_lat"], p_bounds["max_lon"]))
        if uf and min_cep and (uf, min_cep) in cep_centroids:
            pts.append(cep_centroids[(uf, min_cep)])
        if uf and max_cep and (uf, max_cep) in cep_centroids:
            pts.append(cep_centroids[(uf, max_cep)])
        if not pts and uf and city and (uf, city) in city_centroids:
            pts.append(city_centroids[(uf, city)])
        if not pts:
            continue

        if p_bounds:
            lat, lon = p_bounds["avg_lat"], p_bounds["avg_lon"]
        else:
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)

        row_bounds = GeoBoundsFilter(
            north=max(p[0] for p in pts),
            south=min(p[0] for p in pts),
            east=max(p[1] for p in pts),
            west=min(p[1] for p in pts),
        )

        if filter_by_viewport and viewport_bounds is not None:
            if not _bounds_intersect(row_bounds, viewport_bounds):
                continue

        coord_key = (round(lat, 5), round(lon, 5))
        cnt = int(r.get("company_count") or 0)
        if cnt <= 0:
            continue
        neighborhood = (r.get("neighborhood") or "").strip() or None
        if coord_key in merged:
            entry = merged[coord_key]
            entry["count"] += cnt
            existing_b = entry["bounds"]
            entry["bounds"] = GeoBoundsFilter(
                north=max(existing_b.north, row_bounds.north),
                south=min(existing_b.south, row_bounds.south),
                east=max(existing_b.east, row_bounds.east),
                west=min(existing_b.west, row_bounds.west),
            )
            if not entry["neighborhood"] and neighborhood:
                entry["neighborhood"] = neighborhood
            if entry["postal_code_prefix"] != cep_prefix:
                entry["postal_code_prefix"] = None
        else:
            label_parts = []
            if neighborhood:
                label_parts.append(neighborhood)
            if cep_prefix:
                suffix = "" if len(cep_prefix) >= 8 else "*"
                label_parts.append(f"CEP {cep_prefix}{suffix}")
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
                "bounds": row_bounds,
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


async def search_map_coordinate_groups(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    spatial_targets: Optional[dict] = None,
    *,
    viewport_bounds: Optional[GeoBoundsFilter] = None,
) -> Tuple[List[MapCoordinateGroup], Optional[MapCenterPoint]]:
    """Group high-zoom dense results by exact resolved coordinate (supporting single, spiderfy, and paginated pins)."""
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

    query = f"""
        SELECT
            CASE
                WHEN cgc.latitude IS NOT NULL
                 AND NOT (csi.mei_optant AND cgc.precision IN ('exact_number', 'street'))
                THEN ROUND(cgc.latitude::numeric, 6)::double precision
                ELSE NULL
            END AS cached_lat,
            CASE
                WHEN cgc.longitude IS NOT NULL
                 AND NOT (csi.mei_optant AND cgc.precision IN ('exact_number', 'street'))
                THEN ROUND(cgc.longitude::numeric, 6)::double precision
                ELSE NULL
            END AS cached_lon,
            CASE
                WHEN cgc.latitude IS NOT NULL
                 AND NOT (csi.mei_optant AND cgc.precision IN ('exact_number', 'street'))
                THEN cgc.precision
                ELSE NULL
            END AS cached_precision,
            CASE
                WHEN cgc.latitude IS NOT NULL
                 AND NOT (csi.mei_optant AND cgc.precision IN ('exact_number', 'street'))
                THEN COALESCE(cgc.source, 'br_company.geo')
                ELSE NULL
            END AS cached_source,
            csi.state,
            csi.municipality_name,
            csi.mei_optant,
            CASE
                WHEN cgc.latitude IS NOT NULL
                 AND NOT (csi.mei_optant AND cgc.precision IN ('exact_number', 'street'))
                THEN NULL
                ELSE NULLIF(regexp_replace(COALESCE(csi.postal_code, ''), '\\D', '', 'g'), '')
            END AS clean_cep,
            CASE
                WHEN (cgc.latitude IS NOT NULL AND NOT (csi.mei_optant AND cgc.precision IN ('exact_number', 'street')))
                  OR csi.mei_optant
                THEN NULL
                ELSE NULLIF(UPPER(TRIM(COALESCE(csi.street_type, ''))), '')
            END AS street_type,
            CASE
                WHEN (cgc.latitude IS NOT NULL AND NOT (csi.mei_optant AND cgc.precision IN ('exact_number', 'street')))
                  OR csi.mei_optant
                THEN NULL
                ELSE NULLIF(UPPER(TRIM(COALESCE(csi.street, ''))), '')
            END AS street,
            CASE
                WHEN (cgc.latitude IS NOT NULL AND NOT (csi.mei_optant AND cgc.precision IN ('exact_number', 'street')))
                  OR csi.mei_optant
                THEN NULL
                ELSE NULLIF(UPPER(TRIM(COALESCE(csi.number, ''))), '')
            END AS number,
            COUNT(*)::int AS company_count,
            (ARRAY_AGG(
                COALESCE(NULLIF(TRIM(csi.trade_name), ''), csi.legal_name)
                ORDER BY csi.start_activity_date DESC NULLS LAST, csi.cnpj ASC
            ))[1:3] AS sample_names,
            (ARRAY_AGG(
                DISTINCT NULLIF(regexp_replace(COALESCE(csi.postal_code, ''), '\\D', '', 'g'), '')
            ))[1:25] AS group_ceps,
            (ARRAY_AGG(
                csi.cnpj
                ORDER BY csi.start_activity_date DESC NULLS LAST, csi.cnpj ASC
            ))[1:12] AS sample_cnpjs
        FROM {source_table} csi
        LEFT JOIN app_saas.company_geo_cache cgc ON cgc.cnpj = csi.cnpj
        WHERE {where_sql}
        GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
        ORDER BY company_count DESC;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()

    uncached_inputs: List[dict] = []
    for idx, r in enumerate(rows):
        if r.get("cached_lat") is None or r.get("cached_lon") is None:
            uncached_inputs.append(
                {
                    "group_id": str(idx),
                    "state": r.get("state"),
                    "municipality_name": r.get("municipality_name"),
                    "postal_code": r.get("clean_cep"),
                    "street_type": r.get("street_type"),
                    "street": r.get("street"),
                    "number": r.get("number"),
                    "mei_optant": bool(r.get("mei_optant")),
                }
            )

    resolved_uncached = await geo_service.resolve_address_groups_geo_batch(
        conn, uncached_inputs, mask_mei_exact=True
    )

    merged: Dict[str, Dict[str, Any]] = {}
    for idx, r in enumerate(rows):
        if r.get("cached_lat") is not None and r.get("cached_lon") is not None:
            lat = float(r["cached_lat"])
            lon = float(r["cached_lon"])
            precision = str(r.get("cached_precision") or "cep_centroid")
            source = str(r.get("cached_source") or geo_service.DEFAULT_GEO_SOURCE)
        else:
            coord = resolved_uncached.get(str(idx))
            if not coord:
                continue
            lat = float(coord["latitude"])
            lon = float(coord["longitude"])
            precision = str(coord["precision"])
            source = str(coord.get("source") or geo_service.DEFAULT_GEO_SOURCE)

        if viewport_bounds is not None and not _point_in_bounds(lat, lon, viewport_bounds):
            continue

        coord_key = f"{lat:.6f},{lon:.6f}"
        cnt = int(r.get("company_count") or 0)
        if cnt <= 0:
            continue
        ceps = [c for c in (r.get("group_ceps") or []) if c]
        names = [n for n in (r.get("sample_names") or []) if n]
        cnpjs = [str(c).strip() for c in (r.get("sample_cnpjs") or []) if c]

        if coord_key in merged:
            entry = merged[coord_key]
            entry["count"] += cnt
            entry["postal_codes"].update(ceps)
            for n in names:
                if len(entry["sample_names"]) < 3 and n not in entry["sample_names"]:
                    entry["sample_names"].append(n)
            if entry["count"] <= 12:
                for c in cnpjs:
                    if c not in entry["sample_cnpjs"] and len(entry["sample_cnpjs"]) < 12:
                        entry["sample_cnpjs"].append(c)
            else:
                entry["sample_cnpjs"] = []
        else:
            merged[coord_key] = {
                "coord_key": coord_key,
                "latitude": lat,
                "longitude": lon,
                "count": cnt,
                "geo_precision": precision,
                "geo_source": source,
                "municipality_name": (r.get("municipality_name") or "").strip() or None,
                "state": geo_service.clean_state(r.get("state")),
                "postal_codes": set(ceps),
                "sample_names": names[:3],
                "sample_cnpjs": cnpjs[:12] if cnt <= 12 else [],
            }

    small_cnpjs: List[str] = []
    for entry in merged.values():
        if entry["count"] <= 12:
            small_cnpjs.extend(entry["sample_cnpjs"])

    items_by_cnpj: Dict[str, Dict[str, Any]] = {}
    if small_cnpjs:
        unique_small = sorted(set(small_cnpjs))
        detail_query = f"""
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
            WHERE csi.cnpj = ANY(%(cnpjs)s::bpchar[]);
        """
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(detail_query, {"cnpjs": unique_small})
            for drow in await cur.fetchall():
                items_by_cnpj[str(drow["cnpj"]).strip()] = drow

    groups: List[MapCoordinateGroup] = []
    for entry in sorted(merged.values(), key=lambda x: x["count"], reverse=True):
        group_items: List[MapCompanyItem] = []
        if entry["count"] <= 12:
            for cnpj in entry["sample_cnpjs"]:
                drow = items_by_cnpj.get(cnpj)
                if not drow:
                    continue
                group_items.append(
                    MapCompanyItem(
                        **_row_to_masked_preview_dict(drow),
                        latitude=entry["latitude"],
                        longitude=entry["longitude"],
                        geo_precision=entry["geo_precision"],
                        geo_source=entry["geo_source"],
                    )
                )
        groups.append(
            MapCoordinateGroup(
                coord_key=entry["coord_key"],
                latitude=entry["latitude"],
                longitude=entry["longitude"],
                count=entry["count"],
                geo_precision=entry["geo_precision"],
                geo_source=entry["geo_source"],
                municipality_name=entry["municipality_name"],
                state=entry["state"],
                postal_codes=sorted(entry["postal_codes"]),
                sample_names=entry["sample_names"],
                items=group_items,
            )
        )

    if groups:
        total_weight = sum(g.count for g in groups) or 1
        center = MapCenterPoint(
            latitude=sum(g.latitude * g.count for g in groups) / total_weight,
            longitude=sum(g.longitude * g.count for g in groups) / total_weight,
        )
    else:
        center = _compute_filter_center(filters, [])

    return groups, center


async def search_map_point_companies(
    conn: psycopg.AsyncConnection,
    filters: SearchFilterRequest,
    *,
    latitude: float,
    longitude: float,
    postal_codes: Optional[List[str]] = None,
    q_point: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> Tuple[int, List[MapCompanyItem]]:
    """Fetch paginated companies located at a specific map coordinate (for dense grouped pins)."""
    use_index = await is_search_index_populated(conn)
    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = await resolve_effective_where_clause(
        conn,
        filters,
        alias="csi",
        use_index_table=use_index,
    )
    if where_sql == "FALSE":
        return 0, []

    extra_sql: List[str] = [f"({where_sql})"]
    clean_ceps = [
        "".join(ch for ch in str(c) if ch.isdigit())
        for c in (postal_codes or [])
        if c
    ]
    clean_ceps = [c for c in clean_ceps if len(c) == 8]
    if clean_ceps:
        params["point_ceps"] = clean_ceps
        extra_sql.append("csi.postal_code = ANY(%(point_ceps)s::bpchar[])")

    if q_point and q_point.strip():
        q_clean = q_point.strip()
        params["q_point_like"] = f"%{q_clean}%"
        q_digits = "".join(ch for ch in q_clean if ch.isdigit())
        if q_digits:
            params["q_point_digits"] = f"%{q_digits}%"
            extra_sql.append(
                "(csi.legal_name ILIKE %(q_point_like)s OR csi.trade_name ILIKE %(q_point_like)s "
                "OR csi.cnpj LIKE %(q_point_digits)s OR csi.main_cnae LIKE %(q_point_digits)s "
                "OR csi.main_cnae_description ILIKE %(q_point_like)s)"
            )
        else:
            extra_sql.append(
                "(csi.legal_name ILIKE %(q_point_like)s OR csi.trade_name ILIKE %(q_point_like)s "
                "OR csi.main_cnae_description ILIKE %(q_point_like)s)"
            )

    combined_where = " AND ".join(extra_sql)
    probe_query = f"""
        SELECT
            csi.cnpj,
            csi.state,
            csi.municipality_name,
            csi.postal_code,
            csi.street_type,
            csi.street,
            csi.number,
            csi.mei_optant
        FROM {source_table} csi
        WHERE {combined_where}
        ORDER BY csi.start_activity_date DESC NULLS LAST, csi.cnpj ASC
        LIMIT 10000;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(probe_query, params)
        probe_rows = await cur.fetchall()

    if not probe_rows:
        return 0, []

    geo_by_cnpj = await geo_service.resolve_establishments_geo_batch(
        conn, probe_rows, mask_mei_exact=True
    )

    tol = 2e-5
    matched_cnpjs: List[str] = []
    for r in probe_rows:
        cnpj = str(r["cnpj"]).strip()
        coord = geo_by_cnpj.get(cnpj)
        if not coord:
            continue
        if (
            abs(float(coord["latitude"]) - latitude) <= tol
            and abs(float(coord["longitude"]) - longitude) <= tol
        ):
            matched_cnpjs.append(cnpj)

    total = len(matched_cnpjs)
    safe_page = max(page, 1)
    safe_size = min(max(page_size, 1), 100)
    start = (safe_page - 1) * safe_size
    page_cnpjs = matched_cnpjs[start : start + safe_size]
    if not page_cnpjs:
        return total, []

    detail_query = f"""
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
        WHERE csi.cnpj = ANY(%(page_cnpjs)s::bpchar[]);
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(detail_query, {"page_cnpjs": page_cnpjs})
        detail_rows = await cur.fetchall()

    by_cnpj = {str(r["cnpj"]).strip(): r for r in detail_rows}
    items: List[MapCompanyItem] = []
    for cnpj in page_cnpjs:
        row = by_cnpj.get(cnpj)
        coord = geo_by_cnpj.get(cnpj)
        if not row or not coord:
            continue
        items.append(
            MapCompanyItem(
                **_row_to_masked_preview_dict(row),
                latitude=float(coord["latitude"]),
                longitude=float(coord["longitude"]),
                geo_precision=str(coord["precision"]),
                geo_source=coord.get("source"),
            )
        )
    return total, items


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
    *,
    zoom: Optional[int] = None,
    viewport_bounds: Optional[GeoBoundsFilter] = None,
) -> Tuple[
    int,
    str,
    List[MapCompanyItem],
    List[MapClusterItem],
    List[MapCoordinateGroup],
    bool,
    Optional[int],
    Optional[MapCenterPoint],
]:
    """Return total count and either pins, coordinate-grouped pins, or server-side clusters."""
    safe_limit = min(max(limit, 1), 2000)
    safe_offset = max(offset, 0)
    wide_viewport = (
        filters.geo_radius is None
        and filters.geo_bounds is None
        and _is_wide_viewport(zoom, viewport_bounds)
    )
    use_spatial_viewport = viewport_bounds is not None and not wide_viewport
    pin_threshold = (
        min(cluster_threshold, MAP_HIGH_ZOOM_PIN_THRESHOLD)
        if (zoom is not None and zoom >= 17)
        else cluster_threshold
    )

    async def _execute_uncached() -> Tuple[
        int,
        str,
        List[MapCompanyItem],
        List[MapClusterItem],
        List[MapCoordinateGroup],
        bool,
        Optional[int],
        Optional[MapCenterPoint],
    ]:
        spatial = None
        if (
            filters.geo_radius is not None
            or filters.geo_bounds is not None
            or use_spatial_viewport
        ):
            spatial = await geo_service.resolve_spatial_filter_targets(
                conn,
                geo_radius=filters.geo_radius,
                geo_bounds=filters.geo_bounds,
                viewport_bounds=viewport_bounds if use_spatial_viewport else None,
            )
            if spatial.get("empty"):
                return 0, "pins", [], [], [], False, None, _compute_filter_center(filters, [])

        total = await count_matching_companies(conn, filters, spatial_targets=spatial)
        if total <= 0:
            return 0, "pins", [], [], [], False, None, _compute_filter_center(filters, [])

        if total > pin_threshold:
            if zoom is not None and zoom >= 17:
                coord_groups, center = await search_map_coordinate_groups(
                    conn,
                    filters,
                    spatial_targets=spatial,
                    viewport_bounds=viewport_bounds,
                )
                group_total = sum(g.count for g in coord_groups)
                return (
                    max(total, group_total) if not viewport_bounds else max(group_total, total),
                    "coordinate_pins",
                    [],
                    [],
                    coord_groups,
                    False,
                    None,
                    center,
                )

            clusters, center = await search_map_clusters(
                conn,
                filters,
                total_companies=total,
                spatial_targets=spatial,
                zoom=zoom,
                viewport_bounds=viewport_bounds,
                filter_by_viewport=wide_viewport,
            )
            cluster_total = sum(c.count for c in clusters)
            return (
                total if wide_viewport else max(total, cluster_total),
                "clusters",
                [],
                clusters,
                [],
                False,
                None,
                center,
            )

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
            [],
            has_more,
            next_off if has_more else None,
            center,
        )

    if ttl_seconds <= 0 or max_entries <= 0:
        return await _execute_uncached()

    epoch = await get_search_cache_epoch(conn)
    filter_key = _normalized_filter_key(filters)
    vp_key = None
    if viewport_bounds is not None:
        decimals = 5 if (zoom is not None and zoom >= 17) else 4
        vp_key = (
            round(viewport_bounds.north, decimals),
            round(viewport_bounds.south, decimals),
            round(viewport_bounds.east, decimals),
            round(viewport_bounds.west, decimals),
        )
    map_key = (epoch, filter_key, safe_offset, safe_limit, zoom, vp_key)
    cached = _cache_get(_search_map_cache, map_key, ttl_seconds)
    if cached is not None:
        return cached

    result = await _execute_uncached()
    _cache_set(_search_map_cache, map_key, result, max_entries)
    return result

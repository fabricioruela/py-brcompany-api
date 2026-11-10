"""Database queries and filter builder for the PanoramaPJ Search Engine."""

from typing import Any, Dict, List, Tuple
import psycopg
from psycopg.rows import dict_row

from brcompany_api.domains.search.schemas import MaskedCompanyPreviewItem, SearchFilterRequest
from brcompany_api.utils.masking import mask_email, mask_phone, mask_street


async def is_search_index_populated(conn: psycopg.AsyncConnection) -> bool:
    """Check whether registry.company_search_index exists and has at least one row."""
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
            return (await cur.fetchone()) is not None
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


async def count_matching_companies(
    conn: psycopg.AsyncConnection, filters: SearchFilterRequest
) -> int:
    """Count total active companies matching the provided filters."""
    use_index = await is_search_index_populated(conn)
    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = build_where_clause(filters, alias="csi", use_index_table=use_index)

    query = f"""
        SELECT COUNT(*)::int AS total
        FROM {source_table} csi
        WHERE {where_sql};
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        row = await cur.fetchone()
        return int(row["total"]) if row else 0


async def preview_matching_companies(
    conn: psycopg.AsyncConnection, filters: SearchFilterRequest, limit: int = 5
) -> List[MaskedCompanyPreviewItem]:
    """Fetch top 5 matching companies with masked contact and street fields."""
    use_index = await is_search_index_populated(conn)
    source_table = "company_search_index" if use_index else FALLBACK_SOURCE_SQL
    where_sql, params = build_where_clause(filters, alias="csi", use_index_table=use_index)
    params["limit"] = min(max(limit, 1), 5)

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
        LIMIT %(limit)s;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        rows = await cur.fetchall()

    items: List[MaskedCompanyPreviewItem] = []
    for r in rows:
        items.append(
            MaskedCompanyPreviewItem(
                cnpj=r["cnpj"],
                cnpj_base=r["cnpj_base"],
                identifier_type=r.get("identifier_type"),
                legal_name=r.get("legal_name") or "RAZAO SOCIAL NAO INFORMADA",
                trade_name=r.get("trade_name"),
                start_activity_date=r.get("start_activity_date"),
                main_cnae=r.get("main_cnae"),
                main_cnae_description=r.get("main_cnae_description"),
                company_size=r.get("company_size"),
                share_capital=r.get("share_capital"),
                simples_optant=bool(r.get("simples_optant")),
                mei_optant=bool(r.get("mei_optant")),
                state=r.get("state"),
                municipality_code=r.get("municipality_code"),
                municipality_name=r.get("municipality_name"),
                neighborhood=r.get("neighborhood"),
                street_masked=mask_street(r.get("street_type"), r.get("street"), r.get("number")),
                phone_masked=mask_phone(r.get("area_code_1"), r.get("phone_1")),
                email_masked=mask_email(r.get("email")),
                has_phone=bool(r.get("has_phone")),
                has_email=bool(r.get("has_email")),
            )
        )
    return items

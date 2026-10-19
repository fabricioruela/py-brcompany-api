"""Database queries and business logic for Company and Establishment domain."""

import logging
from typing import List, Optional
import psycopg
from psycopg.rows import dict_row

from brcompany_api.domains.company.schemas import (
    CnaeItem,
    CompanyBaseInfo,
    CompanyProfileResponse,
    EstablishmentSummary,
    PartnerItem,
)
from brcompany_api.utils.cnpj import clean_cnpj

logger = logging.getLogger(__name__)


async def get_establishment_by_cnpj(
    conn: psycopg.AsyncConnection, cnpj: str
) -> Optional[dict]:
    """Fetch establishment summary by 14-digit CNPJ using unique index."""
    query = """
        SELECT
            cnpj,
            cnpj_base,
            cnpj_order,
            cnpj_check_digits,
            identifier_type,
            trade_name,
            registration_status,
            registration_status_reason_description,
            registration_status_date,
            start_activity_date,
            main_cnae,
            main_cnae_description,
            secondary_cnaes,
            street_type,
            street,
            number,
            complement,
            neighborhood,
            postal_code,
            state,
            municipality_code,
            municipality_name,
            area_code_1,
            phone_1,
            email
        FROM v_establishment
        WHERE cnpj = %(cnpj)s
        LIMIT 1;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, {"cnpj": cnpj})
        return await cur.fetchone()


async def get_headquarter_or_first_establishment(
    conn: psycopg.AsyncConnection, cnpj_base: str
) -> Optional[dict]:
    """Fetch headquarter (matriz) or the first registered establishment for an 8-digit base."""
    query = """
        SELECT
            cnpj,
            cnpj_base,
            cnpj_order,
            cnpj_check_digits,
            identifier_type,
            trade_name,
            registration_status,
            registration_status_reason_description,
            registration_status_date,
            start_activity_date,
            main_cnae,
            main_cnae_description,
            secondary_cnaes,
            street_type,
            street,
            number,
            complement,
            neighborhood,
            postal_code,
            state,
            municipality_code,
            municipality_name,
            area_code_1,
            phone_1,
            email
        FROM v_establishment
        WHERE cnpj_base = %(cnpj_base)s
        ORDER BY identifier_type ASC, cnpj_order ASC
        LIMIT 1;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, {"cnpj_base": cnpj_base})
        return await cur.fetchone()


async def get_company_by_base(
    conn: psycopg.AsyncConnection, cnpj_base: str
) -> Optional[dict]:
    """Fetch base company registration and tax regime info by 8-digit base CNPJ."""
    query = """
        SELECT
            cnpj_base,
            legal_name,
            legal_nature_code,
            legal_nature_description,
            responsible_qualification_code,
            responsible_qualification_description,
            share_capital,
            company_size,
            responsible_federative_entity,
            simples_optant,
            simples_opt_in_date,
            simples_opt_out_date,
            mei_optant,
            mei_opt_in_date,
            mei_opt_out_date
        FROM v_company_detail
        WHERE cnpj_base = %(cnpj_base)s
        LIMIT 1;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, {"cnpj_base": cnpj_base})
        return await cur.fetchone()


async def get_partners_by_base(
    conn: psycopg.AsyncConnection, cnpj_base: str
) -> List[dict]:
    """Fetch all partners (QSA) associated with an 8-digit base CNPJ."""
    query = """
        SELECT
            p.id,
            p.cnpj_base,
            p.partner_type,
            p.partner_name,
            p.partner_tax_id,
            p.qualification_code,
            pq.description AS qualification_description,
            p.entry_date,
            p.country_code,
            c.description AS country_name,
            p.legal_representative_tax_id,
            p.legal_representative_name,
            p.legal_representative_qualification_code,
            p.age_group
        FROM partner p
        LEFT JOIN partner_qualification pq ON pq.code = p.qualification_code
        LEFT JOIN country c ON c.code = p.country_code
        WHERE p.cnpj_base = %(cnpj_base)s
        ORDER BY p.id ASC;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, {"cnpj_base": cnpj_base})
        return await cur.fetchall()


async def get_cnaes_descriptions(
    conn: psycopg.AsyncConnection, codes: List[str]
) -> List[CnaeItem]:
    """Fetch descriptions for a list of CNAE codes."""
    if not codes:
        return []
    query = """
        SELECT code, description
        FROM cnae
        WHERE code = ANY(%(codes)s)
        ORDER BY code ASC;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, {"codes": codes})
        rows = await cur.fetchall()
        return [CnaeItem(code=r["code"], description=r["description"]) for r in rows]


async def count_branches(
    conn: psycopg.AsyncConnection, cnpj_base: str
) -> int:
    """Count how many establishments are registered under a base CNPJ."""
    query = "SELECT count(*) AS total FROM establishment WHERE cnpj_base = %(cnpj_base)s;"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, {"cnpj_base": cnpj_base})
        row = await cur.fetchone()
        return row["total"] if row else 0


async def get_branches_by_base(
    conn: psycopg.AsyncConnection,
    cnpj_base: str,
    limit: int = 50,
    offset: int = 0,
) -> List[dict]:
    """Fetch paginated establishments/branches for a base CNPJ."""
    query = """
        SELECT
            cnpj,
            cnpj_base,
            cnpj_order,
            cnpj_check_digits,
            identifier_type,
            trade_name,
            registration_status,
            registration_status_reason_description,
            registration_status_date,
            start_activity_date,
            main_cnae,
            main_cnae_description,
            secondary_cnaes,
            street_type,
            street,
            number,
            complement,
            neighborhood,
            postal_code,
            state,
            municipality_code,
            municipality_name,
            area_code_1,
            phone_1,
            email
        FROM v_establishment
        WHERE cnpj_base = %(cnpj_base)s
        ORDER BY identifier_type ASC, cnpj_order ASC
        LIMIT %(limit)s OFFSET %(offset)s;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, {"cnpj_base": cnpj_base, "limit": limit, "offset": offset})
        return await cur.fetchall()


async def get_company_profile(
    conn: psycopg.AsyncConnection, raw_cnpj: str
) -> Optional[CompanyProfileResponse]:
    """Fetch consolidated company profile including establishment, company base, and QSA."""
    clean = clean_cnpj(raw_cnpj)

    if len(clean) == 14:
        est_data = await get_establishment_by_cnpj(conn, clean)
        if not est_data:
            return None
        cnpj_base = est_data["cnpj_base"]
    elif len(clean) == 8:
        cnpj_base = clean
        est_data = await get_headquarter_or_first_establishment(conn, cnpj_base)
        if not est_data:
            return None
    else:
        return None

    company_data = await get_company_by_base(conn, cnpj_base)
    if not company_data:
        # Construct fallback minimal company data if establishment exists
        company_data = {
            "cnpj_base": cnpj_base,
            "legal_name": est_data.get("trade_name") or "Razão Social não informada",
        }

    partners_data = await get_partners_by_base(conn, cnpj_base)
    total_branches = await count_branches(conn, cnpj_base)

    # Resolve secondary CNAEs details if present
    secondary_cnaes = est_data.get("secondary_cnaes") or []
    cnae_details = await get_cnaes_descriptions(conn, secondary_cnaes)

    return CompanyProfileResponse(
        company=CompanyBaseInfo.model_validate(company_data),
        establishment=EstablishmentSummary.model_validate(est_data),
        secondary_cnaes_details=cnae_details,
        partners=[PartnerItem.model_validate(p) for p in partners_data],
        total_branches=total_branches,
    )

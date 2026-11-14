"""Service functions for metadata domains (CNAE, municipalities, legal natures) and ETL status."""

from typing import List, Optional
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from brcompany_api.utils.geo import BRAZIL_STATES as _BRAZIL_STATES_SET, normalize_place_name


async def get_cnaes(
    conn: psycopg.AsyncConnection, q: Optional[str] = None, limit: int = 100
) -> List[dict]:
    """Retrieve CNAE economic activities, optionally filtering by description or code."""
    if q:
        query = """
            SELECT code, description
            FROM cnae
            WHERE code ILIKE %(search)s OR description ILIKE %(search)s
            ORDER BY code ASC
            LIMIT %(limit)s;
        """
        params = {"search": f"%{q}%", "limit": limit}
    else:
        query = "SELECT code, description FROM cnae ORDER BY code ASC LIMIT %(limit)s;"
        params = {"limit": limit}

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def get_municipalities(
    conn: psycopg.AsyncConnection, q: Optional[str] = None, limit: int = 100
) -> List[dict]:
    """Retrieve municipalities, optionally filtering by name or code."""
    if q:
        query = """
            SELECT code, description
            FROM municipality
            WHERE code ILIKE %(search)s OR description ILIKE %(search)s
            ORDER BY description ASC
            LIMIT %(limit)s;
        """
        params = {"search": f"%{q}%", "limit": limit}
    else:
        query = "SELECT code, description FROM municipality ORDER BY description ASC LIMIT %(limit)s;"
        params = {"limit": limit}

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def get_legal_natures(conn: psycopg.AsyncConnection) -> List[dict]:
    """Retrieve all legal nature codes and descriptions."""
    query = "SELECT code, description FROM legal_nature ORDER BY code ASC;"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query)
        return await cur.fetchall()


BRAZIL_STATES = [
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA",
    "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN",
    "RO", "RR", "RS", "SC", "SE", "SP", "TO",
]


async def get_locations_cascade(
    conn: psycopg.AsyncConnection,
    state: Optional[str] = None,
    municipality_code: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 1000,
) -> dict:
    """Retrieve UF -> Municipality -> Neighborhood cascade from location_hierarchy (with fallback)."""
    clean_state = state.strip().upper()[:2] if state and state.strip() else None
    clean_mun = municipality_code.strip()[:4] if municipality_code and municipality_code.strip() else None

    municipalities: List[dict] = []
    neighborhoods: List[str] = []

    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT 1 FROM location_hierarchy LIMIT 1;")
            has_hierarchy = (await cur.fetchone()) is not None

            if has_hierarchy and clean_state:
                if q and q.strip():
                    await cur.execute(
                        """
                        SELECT municipality_code AS code, MAX(municipality_name) AS description
                        FROM location_hierarchy
                        WHERE state = %(state)s
                          AND municipality_name ILIKE %(q)s
                        GROUP BY municipality_code
                        ORDER BY MAX(municipality_name) ASC
                        LIMIT %(limit)s;
                        """,
                        {"state": clean_state, "q": f"%{q.strip()}%", "limit": limit},
                    )
                else:
                    await cur.execute(
                        """
                        SELECT municipality_code AS code, MAX(municipality_name) AS description
                        FROM location_hierarchy
                        WHERE state = %(state)s
                        GROUP BY municipality_code
                        ORDER BY MAX(municipality_name) ASC
                        LIMIT %(limit)s;
                        """,
                        {"state": clean_state, "limit": limit},
                    )
                municipalities = await cur.fetchall()

                if clean_mun:
                    await cur.execute(
                        """
                        SELECT neighborhood
                        FROM location_hierarchy
                        WHERE state = %(state)s
                          AND municipality_code = %(mun)s
                          AND neighborhood <> ''
                        ORDER BY neighborhood ASC
                        LIMIT %(limit)s;
                        """,
                        {"state": clean_state, "mun": clean_mun, "limit": limit},
                    )
                    neighborhoods = [r["neighborhood"] for r in await cur.fetchall() if r.get("neighborhood")]
            elif clean_state:
                municipalities = await get_municipalities(conn, q=q, limit=limit)
    except Exception:
        await conn.rollback()
        if clean_state:
            municipalities = await get_municipalities(conn, q=q, limit=limit)

    return {
        "states": BRAZIL_STATES,
        "municipalities": municipalities,
        "neighborhoods": neighborhoods,
    }


async def _get_table_row_count(
    conn: psycopg.AsyncConnection, schema: str, table: str
) -> int:
    """Retrieve fast row count estimate from pg_class, falling back to exact COUNT(*) when unanalyzed."""
    estimate_query = """
        SELECT c.reltuples::bigint AS estimate
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %(schema)s AND c.relname = %(table)s
        LIMIT 1;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(estimate_query, {"schema": schema, "table": table})
        row = await cur.fetchone()
        if row and row["estimate"] is not None and row["estimate"] > 0:
            return int(row["estimate"])

        exact_query = sql.SQL("SELECT count(*) AS total FROM {}.{};").format(
            sql.Identifier(schema),
            sql.Identifier(table),
        )
        await cur.execute(exact_query)
        exact_row = await cur.fetchone()
        return int(exact_row["total"]) if exact_row else 0


async def get_etl_status(conn: psycopg.AsyncConnection, schema: str) -> dict:
    """Retrieve latest published ETL load run metadata and table metrics."""
    load_run_query = """
        SELECT
            month,
            publish_mode,
            status,
            started_at,
            finished_at
        FROM etl.load_run
        WHERE status = 'published'
        ORDER BY id DESC
        LIMIT 1;
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(load_run_query)
        run = await cur.fetchone()

    total_companies = await _get_table_row_count(conn, schema, "company")
    total_establishments = await _get_table_row_count(conn, schema, "establishment")

    return {
        "current_schema": schema,
        "last_published_month": run["month"] if run else None,
        "publish_mode": run["publish_mode"] if run else None,
        "status": run["status"] if run else "none",
        "started_at": run["started_at"] if run else None,
        "finished_at": run["finished_at"] if run else None,
        "total_companies": total_companies,
        "total_establishments": total_establishments,
    }


async def _lookup_nearest_geo_address(
    conn: psycopg.AsyncConnection, latitude: float, longitude: float
) -> Optional[dict]:
    """Find nearest Brazilian address in geo.addresses using bounded PostGIS KNN search."""
    states = sorted(_BRAZIL_STATES_SET)
    queries = (
        """
        SELECT region, city
        FROM geo.addresses
        WHERE geom IS NOT NULL
          AND region = ANY(%(states)s)
          AND city IS NOT NULL
          AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 0.5)
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)
        LIMIT 1;
        """,
        """
        SELECT region, city
        FROM geo.addresses
        WHERE geom IS NOT NULL
          AND region = ANY(%(states)s)
          AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), 1.5)
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)
        LIMIT 1;
        """,
    )
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            for query in queries:
                await cur.execute(query, {"lat": latitude, "lon": longitude, "states": states})
                row = await cur.fetchone()
                if row:
                    return row
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            pass
    return None


async def reverse_geocode_coordinates(
    conn: psycopg.AsyncConnection, latitude: float, longitude: float
) -> dict:
    """Resolve browser coordinates via geo.addresses into RFB UF and 4-digit municipality_code."""
    match = await _lookup_nearest_geo_address(conn, latitude, longitude)
    if not match:
        return {
            "detected": False,
            "state": None,
            "municipality_code": None,
            "municipality_name": None,
            "source": "unavailable",
        }

    state = (match.get("region") or "").strip().upper()[:2]
    if state not in _BRAZIL_STATES_SET:
        return {
            "detected": False,
            "state": None,
            "municipality_code": None,
            "municipality_name": None,
            "source": "unavailable",
        }

    city_name = match.get("city")
    if city_name:
        target_city = normalize_place_name(city_name)
        if target_city:
            try:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT municipality_code, MAX(municipality_name) AS municipality_name
                        FROM location_hierarchy
                        WHERE state = %(state)s
                        GROUP BY municipality_code;
                        """,
                        {"state": state},
                    )
                    rows = await cur.fetchall()
                for row in rows:
                    m_name = row.get("municipality_name") or ""
                    if normalize_place_name(m_name) == target_city:
                        return {
                            "detected": True,
                            "state": state,
                            "municipality_code": row["municipality_code"],
                            "municipality_name": m_name,
                            "source": "geo_addresses_city",
                        }
            except Exception:
                try:
                    await conn.rollback()
                except Exception:
                    pass

    return {
        "detected": True,
        "state": state,
        "municipality_code": None,
        "municipality_name": None,
        "source": "geo_addresses_state",
    }

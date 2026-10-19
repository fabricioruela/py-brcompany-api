"""Service functions for metadata domains (CNAE, municipalities, legal natures) and ETL status."""

from typing import List, Optional
import psycopg
from psycopg.rows import dict_row


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

        # Get counts from active schema
        await cur.execute(f"SELECT count(*) AS total FROM {schema}.company;")
        comp_row = await cur.fetchone()

        await cur.execute(f"SELECT count(*) AS total FROM {schema}.establishment;")
        est_row = await cur.fetchone()

        return {
            "current_schema": schema,
            "last_published_month": run["month"] if run else None,
            "publish_mode": run["publish_mode"] if run else None,
            "status": run["status"] if run else "none",
            "started_at": run["started_at"] if run else None,
            "finished_at": run["finished_at"] if run else None,
            "total_companies": comp_row["total"] if comp_row else 0,
            "total_establishments": est_row["total"] if est_row else 0,
        }

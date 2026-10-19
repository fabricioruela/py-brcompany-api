"""Database connection pool and async connection lifecycle management."""

import logging
import time
from typing import AsyncGenerator, Optional
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from brcompany_api.config import Settings, get_settings

logger = logging.getLogger(__name__)

_pool: Optional[AsyncConnectionPool] = None


async def init_db_pool(settings: Optional[Settings] = None) -> AsyncConnectionPool:
    """Initialize and open the async PostgreSQL connection pool."""
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool

    cfg = settings or get_settings()
    logger.info(
        "Initializing PostgreSQL connection pool (schema: %s, pool size: %d-%d)",
        cfg.app_db_schema,
        cfg.app_db_pool_min_size,
        cfg.app_db_pool_max_size,
    )

    options = f"-c search_path={cfg.app_db_schema},public"
    _pool = AsyncConnectionPool(
        conninfo=cfg.app_database_url,
        min_size=cfg.app_db_pool_min_size,
        max_size=cfg.app_db_pool_max_size,
        timeout=cfg.app_db_pool_timeout,
        kwargs={
            "row_factory": dict_row,
            "options": options,
        },
        open=False,
    )
    await _pool.open()
    logger.info("PostgreSQL connection pool initialized and opened successfully")
    return _pool


async def close_db_pool() -> None:
    """Close the async PostgreSQL connection pool."""
    global _pool
    if _pool is not None:
        logger.info("Closing PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL connection pool closed")


def get_db_pool() -> AsyncConnectionPool:
    """Retrieve the current active connection pool."""
    if _pool is None or _pool.closed:
        raise RuntimeError("Database connection pool is not initialized or has been closed")
    return _pool


async def get_db_conn() -> AsyncGenerator[psycopg.AsyncConnection, None]:
    """FastAPI dependency yielding a checked-out async connection from the pool."""
    pool = get_db_pool()
    async with pool.connection() as conn:
        yield conn


async def check_db_health() -> dict:
    """Perform a lightweight ping to verify PostgreSQL connectivity."""
    start_time = time.perf_counter()
    pool = get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 AS alive;")
            row = await cur.fetchone()
            latency_ms = (time.perf_counter() - start_time) * 1000
            return {
                "status": "healthy" if row and row.get("alive") == 1 else "unhealthy",
                "latency_ms": round(latency_ms, 2),
            }

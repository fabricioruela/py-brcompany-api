"""Database migration runner for the app_saas schema."""

import logging
from pathlib import Path
import psycopg

logger = logging.getLogger(__name__)

SQL_DIR = Path(__file__).parent / "sql"


async def apply_saas_migrations(conn: psycopg.AsyncConnection) -> None:
    """Apply idempotent SQL migrations for the app_saas schema."""
    sql_path = SQL_DIR / "001_app_saas.sql"
    if not sql_path.is_file():
        logger.warning("Migration file not found: %s", sql_path)
        return

    sql_content = sql_path.read_text(encoding="utf-8")
    async with conn.cursor() as cur:
        await cur.execute(sql_content)
    await conn.commit()
    logger.info("Applied app_saas schema migrations successfully.")

"""Georeferencing service bridging RFB establishments (registry) with br_company.geo (geo.addresses)."""

import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import psycopg
from psycopg.rows import dict_row

from brcompany_api.utils.geo import BRAZIL_STATES, normalize_place_name

logger = logging.getLogger(__name__)

GOOGLE_GEOCODING_SOURCE_NAME = "google/geocoding_api"
GOOGLE_CACHE_SOURCE = "google_geocoding"
DEFAULT_GEO_SOURCE = "br_company.geo"

_cached_google_source_id: Optional[int] = None

_STREET_TYPE_ABBR = {
    "RUA": "r",
    "AVENIDA": "av",
    "TRAVESSA": "tv",
    "ALAMEDA": "al",
    "PRACA": "pc",
    "RODOVIA": "rod",
    "ESTRADA": "est",
    "LARGO": "lg",
}


def clean_postal_code(value: Optional[str]) -> Optional[str]:
    """Extract an 8-digit Brazilian CEP or return None when invalid/placeholder."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 8 or digits == "00000000":
        return None
    return digits


def clean_state(value: Optional[str]) -> Optional[str]:
    """Normalize a 2-letter UF against known Brazilian states."""
    if not value:
        return None
    uf = str(value).strip().upper()[:2]
    return uf if uf in BRAZIL_STATES else None


def clean_street_number(value: Optional[str]) -> Optional[str]:
    """Normalize house/building numbers, ignoring common placeholders."""
    if not value:
        return None
    cleaned = " ".join(str(value).strip().upper().split())
    if not cleaned or cleaned in {"SN", "S/N", "S.N", "0", "SEM NUMERO", "***"}:
        return None
    return cleaned


def build_street_candidates(street_type: Optional[str], street: Optional[str]) -> List[str]:
    """Generate lowercase normalized street candidates for geo.addresses matching."""
    st_name = " ".join((street or "").strip().lower().split())
    if not st_name:
        return []
    st_type_raw = " ".join((street_type or "").strip().upper().split())
    candidates: List[str] = []
    if st_type_raw:
        candidates.append(f"{st_type_raw.lower()} {st_name}")
        abbr = _STREET_TYPE_ABBR.get(st_type_raw)
        if abbr:
            candidates.append(f"{abbr} {st_name}")
            candidates.append(f"{abbr}. {st_name}")
    candidates.append(st_name)
    seen = set()
    unique: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


async def _safe_rollback(conn: psycopg.AsyncConnection) -> None:
    try:
        await conn.rollback()
    except Exception:
        pass


async def get_google_source_id(
    conn: psycopg.AsyncConnection, *, create_if_missing: bool = False
) -> Optional[int]:
    """Lookup (and optionally create) the geo.sources row for Google Geocoding API."""
    global _cached_google_source_id
    if _cached_google_source_id is not None:
        return _cached_google_source_id
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT source_id
                FROM geo.sources
                WHERE source_name = %(name)s
                LIMIT 1;
                """,
                {"name": GOOGLE_GEOCODING_SOURCE_NAME},
            )
            row = await cur.fetchone()
            if row and row.get("source_id") is not None:
                _cached_google_source_id = int(row["source_id"])
                return _cached_google_source_id
            if not create_if_missing:
                return None
            await cur.execute(
                """
                INSERT INTO geo.sources (source_name, country, layer, status)
                VALUES (%(name)s, 'BR', 'addresses', 'skipped')
                ON CONFLICT (source_name) DO UPDATE SET updated_at = now()
                RETURNING source_id;
                """,
                {"name": GOOGLE_GEOCODING_SOURCE_NAME},
            )
            created = await cur.fetchone()
            if created and created.get("source_id") is not None:
                source_id = int(created["source_id"])
                await conn.commit()
                _cached_google_source_id = source_id
                return _cached_google_source_id
    except Exception as exc:
        logger.debug("geo.sources google lookup skipped: %s", exc)
        await _safe_rollback(conn)
    return None


async def _lookup_company_cache(
    conn: psycopg.AsyncConnection, cnpjs: Sequence[str]
) -> Dict[str, dict]:
    if not cnpjs:
        return {}
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT cnpj, latitude, longitude, precision, source
                FROM app_saas.company_geo_cache
                WHERE cnpj = ANY(%(cnpjs)s);
                """,
                {"cnpjs": list(cnpjs)},
            )
            rows = await cur.fetchall()
        return {
            r["cnpj"]: {
                "latitude": float(r["latitude"]),
                "longitude": float(r["longitude"]),
                "precision": r["precision"],
                "source": r.get("source") or DEFAULT_GEO_SOURCE,
            }
            for r in rows
            if r.get("latitude") is not None and r.get("longitude") is not None
        }
    except Exception as exc:
        logger.debug("company_geo_cache lookup skipped: %s", exc)
        await _safe_rollback(conn)
        return {}


async def _save_company_cache_entries(
    conn: psycopg.AsyncConnection, entries: Sequence[Tuple[Any, ...]]
) -> None:
    if not entries:
        return
    normalized = [
        (
            e[0],
            e[1],
            e[2],
            e[3],
            e[4],
            e[5] if len(e) > 5 and e[5] else DEFAULT_GEO_SOURCE,
        )
        for e in entries
    ]
    try:
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO app_saas.company_geo_cache (cnpj, latitude, longitude, precision, postal_code, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (cnpj) DO NOTHING;
                """,
                normalized,
            )
        await conn.commit()
    except Exception as exc:
        logger.debug("company_geo_cache insert skipped: %s", exc)
        await _safe_rollback(conn)


async def upsert_company_cache_entry(
    conn: psycopg.AsyncConnection,
    *,
    cnpj: str,
    latitude: float,
    longitude: float,
    precision: str,
    postal_code: Optional[str],
    source: str,
    commit: bool = True,
) -> None:
    """Explicitly overwrite a single CNPJ entry in app_saas.company_geo_cache."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO app_saas.company_geo_cache (cnpj, latitude, longitude, precision, postal_code, source, resolved_at)
            VALUES (%(cnpj)s, %(lat)s, %(lon)s, %(precision)s, %(cep)s, %(source)s, clock_timestamp())
            ON CONFLICT (cnpj) DO UPDATE
            SET latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                precision = EXCLUDED.precision,
                postal_code = COALESCE(EXCLUDED.postal_code, app_saas.company_geo_cache.postal_code),
                source = EXCLUDED.source,
                resolved_at = clock_timestamp();
            """,
            {
                "cnpj": cnpj,
                "lat": latitude,
                "lon": longitude,
                "precision": precision,
                "cep": postal_code,
                "source": source,
            },
        )
    if commit:
        await conn.commit()


async def _lookup_postcode_centroids_batch(
    conn: psycopg.AsyncConnection, pairs: Sequence[Tuple[str, str]]
) -> Dict[Tuple[str, str], Tuple[float, float]]:
    """Resolve (region, postcode_digits) centroids using cache first and geo.addresses fallback."""
    unique_pairs = sorted({(uf, cep) for uf, cep in pairs if uf in BRAZIL_STATES and cep})
    if not unique_pairs:
        return {}

    result: Dict[Tuple[str, str], Tuple[float, float]] = {}
    regions_in = [p[0] for p in unique_pairs]
    ceps_in = [p[1] for p in unique_pairs]

    # 1. Check app_saas.geo_postcode_cache
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT c.region, c.postcode_digits, c.latitude, c.longitude
                FROM app_saas.geo_postcode_cache c
                JOIN unnest(%(regions)s::text[], %(ceps)s::text[]) AS p(region, cep)
                  ON c.region = p.region AND c.postcode_digits = p.cep;
                """,
                {"regions": regions_in, "ceps": ceps_in},
            )
            for r in await cur.fetchall():
                result[(r["region"], r["postcode_digits"])] = (
                    float(r["latitude"]),
                    float(r["longitude"]),
                )
    except Exception as exc:
        logger.debug("geo_postcode_cache lookup skipped: %s", exc)
        await _safe_rollback(conn)

    missing = [p for p in unique_pairs if p not in result]
    if not missing:
        return result

    # 2. Resolve missing CEPs via LATERAL bounded lookup on geo.addresses
    miss_regions = [p[0] for p in missing]
    miss_ceps = [p[1] for p in missing]
    to_cache: List[Tuple[str, str, float, float, int]] = []
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    p.region,
                    p.cep AS postcode_digits,
                    g.lat,
                    g.lon,
                    g.cnt
                FROM unnest(%(regions)s::text[], %(ceps)s::text[]) AS p(region, cep)
                JOIN LATERAL (
                    SELECT
                        AVG(s.latitude) AS lat,
                        AVG(s.longitude) AS lon,
                        COUNT(*)::int AS cnt
                    FROM (
                        SELECT a.latitude, a.longitude
                        FROM geo.addresses a
                        WHERE a.region = p.region
                          AND a.postcode_digits = p.cep
                          AND a.geom IS NOT NULL
                        LIMIT 50
                    ) s
                    HAVING COUNT(*) > 0
                ) g ON TRUE;
                """,
                {"regions": miss_regions, "ceps": miss_ceps},
            )
            for r in await cur.fetchall():
                if r.get("lat") is not None and r.get("lon") is not None:
                    lat = float(r["lat"])
                    lon = float(r["lon"])
                    cnt = int(r.get("cnt") or 1)
                    result[(r["region"], r["postcode_digits"])] = (lat, lon)
                    to_cache.append((r["region"], r["postcode_digits"], lat, lon, cnt))
    except Exception as exc:
        logger.debug("geo.addresses batch CEP lookup skipped: %s", exc)
        await _safe_rollback(conn)

    if to_cache:
        try:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO app_saas.geo_postcode_cache (region, postcode_digits, latitude, longitude, point_count)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (region, postcode_digits) DO NOTHING;
                    """,
                    to_cache,
                )
            await conn.commit()
        except Exception as exc:
            logger.debug("geo_postcode_cache insert skipped: %s", exc)
            await _safe_rollback(conn)

    return result


async def _lookup_city_centroids_batch(
    conn: psycopg.AsyncConnection, pairs: Sequence[Tuple[str, str]]
) -> Dict[Tuple[str, str], Tuple[float, float]]:
    """Fallback centroid resolution by (UF, normalized city name) against geo.addresses."""
    unique_pairs = sorted({(uf, city.strip()) for uf, city in pairs if uf in BRAZIL_STATES and city and city.strip()})
    if not unique_pairs:
        return {}

    result: Dict[Tuple[str, str], Tuple[float, float]] = {}
    regions_in = [p[0] for p in unique_pairs]
    cities_in = [p[1] for p in unique_pairs]
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    p.region,
                    p.city,
                    g.lat,
                    g.lon
                FROM unnest(%(regions)s::text[], %(cities)s::text[]) AS p(region, city)
                JOIN LATERAL (
                    SELECT AVG(s.latitude) AS lat, AVG(s.longitude) AS lon, COUNT(*)::int AS cnt
                    FROM (
                        SELECT a.latitude, a.longitude
                        FROM geo.addresses a
                        WHERE a.region = p.region
                          AND geo.immutable_unaccent(lower(a.city)) = geo.immutable_unaccent(lower(p.city))
                          AND a.geom IS NOT NULL
                        LIMIT 100
                    ) s
                    HAVING COUNT(*) > 0
                ) g ON TRUE;
                """,
                {"regions": regions_in, "cities": cities_in},
            )
            for r in await cur.fetchall():
                if r.get("lat") is not None and r.get("lon") is not None:
                    result[(r["region"], r["city"])] = (float(r["lat"]), float(r["lon"]))
    except Exception as exc:
        logger.debug("geo.addresses batch city lookup skipped: %s", exc)
        await _safe_rollback(conn)
    return result


async def _lookup_exact_number_by_cep_batch(
    conn: psycopg.AsyncConnection,
    triples: Sequence[Tuple[str, str, str]],
    google_source_id: Optional[int],
) -> Dict[Tuple[str, str, str], Tuple[float, float, str]]:
    """Batch exact-number lookup by (region, postcode_digits, number), preferring Google rows."""
    unique_triples = sorted({(uf, cep, num) for uf, cep, num in triples if uf in BRAZIL_STATES and cep and num})
    if not unique_triples:
        return {}

    result: Dict[Tuple[str, str, str], Tuple[float, float, str]] = {}
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    p.region,
                    p.cep,
                    p.num,
                    g.latitude,
                    g.longitude,
                    g.source_id
                FROM unnest(%(regions)s::text[], %(ceps)s::text[], %(nums)s::text[]) AS p(region, cep, num)
                JOIN LATERAL (
                    SELECT a.latitude, a.longitude, a.source_id
                    FROM geo.addresses a
                    WHERE a.region = p.region
                      AND a.postcode_digits = p.cep
                      AND upper(trim(a.number)) = p.num
                      AND a.geom IS NOT NULL
                    ORDER BY COALESCE(a.source_id = %(google_source_id)s, FALSE) DESC, a.created_at DESC
                    LIMIT 1
                ) g ON TRUE;
                """,
                {
                    "regions": [t[0] for t in unique_triples],
                    "ceps": [t[1] for t in unique_triples],
                    "nums": [t[2] for t in unique_triples],
                    "google_source_id": google_source_id,
                },
            )
            for r in await cur.fetchall():
                if r.get("latitude") is not None and r.get("longitude") is not None:
                    src = (
                        GOOGLE_CACHE_SOURCE
                        if google_source_id is not None and r.get("source_id") == google_source_id
                        else DEFAULT_GEO_SOURCE
                    )
                    result[(r["region"], r["cep"], r["num"])] = (
                        float(r["latitude"]),
                        float(r["longitude"]),
                        src,
                    )
    except Exception as exc:
        logger.debug("geo.addresses batch exact CEP+number lookup skipped: %s", exc)
        await _safe_rollback(conn)
    return result


async def _lookup_exact_number_by_street_batch(
    conn: psycopg.AsyncConnection,
    items: Sequence[Tuple[str, str, List[str], str]],
    google_source_id: Optional[int],
) -> Dict[Tuple[str, str, str, str], Tuple[float, float, str]]:
    """Batch exact-number lookup by (region, city, street_candidates, number)."""
    flat_rows: List[Tuple[str, str, str, str, str]] = []
    seen = set()
    for uf, city, candidates, num in items:
        if uf not in BRAZIL_STATES or not city or not candidates or not num:
            continue
        primary_street = candidates[0]
        for cand in candidates:
            key = (uf, city, cand, num)
            if key not in seen:
                seen.add(key)
                flat_rows.append((uf, city, cand, num, primary_street))
    if not flat_rows:
        return {}

    result: Dict[Tuple[str, str, str, str], Tuple[float, float, str]] = {}
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    p.region,
                    p.city,
                    p.primary_street,
                    p.num,
                    g.latitude,
                    g.longitude,
                    g.source_id
                FROM unnest(
                    %(regions)s::text[],
                    %(cities)s::text[],
                    %(streets)s::text[],
                    %(nums)s::text[],
                    %(primaries)s::text[]
                ) AS p(region, city, street, num, primary_street)
                JOIN LATERAL (
                    SELECT a.latitude, a.longitude, a.source_id
                    FROM geo.addresses a
                    WHERE a.region = p.region
                      AND geo.immutable_unaccent(lower(a.city)) = geo.immutable_unaccent(lower(p.city))
                      AND geo.immutable_unaccent(lower(a.street)) = geo.immutable_unaccent(lower(p.street))
                      AND upper(trim(a.number)) = p.num
                      AND a.geom IS NOT NULL
                    ORDER BY COALESCE(a.source_id = %(google_source_id)s, FALSE) DESC, a.created_at DESC
                    LIMIT 1
                ) g ON TRUE;
                """,
                {
                    "regions": [r[0] for r in flat_rows],
                    "cities": [r[1] for r in flat_rows],
                    "streets": [r[2] for r in flat_rows],
                    "nums": [r[3] for r in flat_rows],
                    "primaries": [r[4] for r in flat_rows],
                    "google_source_id": google_source_id,
                },
            )
            for r in await cur.fetchall():
                if r.get("latitude") is not None and r.get("longitude") is not None:
                    key = (r["region"], r["city"], r["primary_street"], r["num"])
                    src = (
                        GOOGLE_CACHE_SOURCE
                        if google_source_id is not None and r.get("source_id") == google_source_id
                        else DEFAULT_GEO_SOURCE
                    )
                    if key not in result or src == GOOGLE_CACHE_SOURCE:
                        result[key] = (float(r["latitude"]), float(r["longitude"]), src)
    except Exception as exc:
        logger.debug("geo.addresses batch exact street+number lookup skipped: %s", exc)
        await _safe_rollback(conn)
    return result


async def _lookup_street_centroids_batch(
    conn: psycopg.AsyncConnection,
    items: Sequence[Tuple[str, str, List[str]]],
    google_source_id: Optional[int],
) -> Dict[Tuple[str, str, str], Tuple[float, float, str]]:
    """Batch street-level lookup by (region, city, street_candidates), preferring Google rows."""
    flat_rows: List[Tuple[str, str, str, str]] = []
    seen = set()
    for uf, city, candidates in items:
        if uf not in BRAZIL_STATES or not city or not candidates:
            continue
        primary_street = candidates[0]
        for cand in candidates:
            key = (uf, city, cand)
            if key not in seen:
                seen.add(key)
                flat_rows.append((uf, city, cand, primary_street))
    if not flat_rows:
        return {}

    result: Dict[Tuple[str, str, str], Tuple[float, float, str]] = {}
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    p.region,
                    p.city,
                    p.primary_street,
                    g.lat,
                    g.lon,
                    g.has_google
                FROM unnest(
                    %(regions)s::text[],
                    %(cities)s::text[],
                    %(streets)s::text[],
                    %(primaries)s::text[]
                ) AS p(region, city, street, primary_street)
                JOIN LATERAL (
                    SELECT
                        AVG(s.latitude) AS lat,
                        AVG(s.longitude) AS lon,
                        BOOL_OR(COALESCE(s.source_id = %(google_source_id)s, FALSE)) AS has_google,
                        COUNT(*)::int AS cnt
                    FROM (
                        SELECT a.latitude, a.longitude, a.source_id
                        FROM geo.addresses a
                        WHERE a.region = p.region
                          AND geo.immutable_unaccent(lower(a.city)) = geo.immutable_unaccent(lower(p.city))
                          AND geo.immutable_unaccent(lower(a.street)) = geo.immutable_unaccent(lower(p.street))
                          AND a.geom IS NOT NULL
                        ORDER BY COALESCE(a.source_id = %(google_source_id)s, FALSE) DESC, a.created_at DESC
                        LIMIT 50
                    ) s
                    HAVING COUNT(*) > 0
                ) g ON TRUE;
                """,
                {
                    "regions": [r[0] for r in flat_rows],
                    "cities": [r[1] for r in flat_rows],
                    "streets": [r[2] for r in flat_rows],
                    "primaries": [r[3] for r in flat_rows],
                    "google_source_id": google_source_id,
                },
            )
            for r in await cur.fetchall():
                if r.get("lat") is not None and r.get("lon") is not None:
                    key = (r["region"], r["city"], r["primary_street"])
                    src = GOOGLE_CACHE_SOURCE if r.get("has_google") else DEFAULT_GEO_SOURCE
                    if key not in result or src == GOOGLE_CACHE_SOURCE:
                        result[key] = (float(r["lat"]), float(r["lon"]), src)
    except Exception as exc:
        logger.debug("geo.addresses batch street lookup skipped: %s", exc)
        await _safe_rollback(conn)
    return result


async def resolve_establishment_geo(
    conn: psycopg.AsyncConnection,
    est: dict,
    *,
    mask_exact: bool = False,
) -> Optional[dict]:
    """Resolve georeferenced coordinates for a single establishment using br_company.geo."""
    cnpj = str(est.get("cnpj") or "").strip()
    uf = clean_state(est.get("state"))
    if not uf:
        return None

    cep = clean_postal_code(est.get("postal_code"))
    number = clean_street_number(est.get("number"))
    city_name = (est.get("municipality_name") or "").strip()
    street_candidates = build_street_candidates(est.get("street_type"), est.get("street"))

    # 1. Check company_geo_cache
    if cnpj:
        cached = (await _lookup_company_cache(conn, [cnpj])).get(cnpj)
        if cached and (not mask_exact or cached["precision"] in ("cep_centroid", "city_centroid")):
            return cached

    google_source_id = await get_google_source_id(conn, create_if_missing=False)

    # 2. Exact number match via CEP (only when not LGPD-masked for MEI)
    if cep and number and not mask_exact:
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT latitude, longitude, source_id
                    FROM geo.addresses
                    WHERE region = %(region)s
                      AND postcode_digits = %(cep)s
                      AND upper(trim(number)) = %(number)s
                      AND geom IS NOT NULL
                    ORDER BY COALESCE(source_id = %(google_source_id)s, FALSE) DESC, created_at DESC
                    LIMIT 1;
                    """,
                    {"region": uf, "cep": cep, "number": number, "google_source_id": google_source_id},
                )
                row = await cur.fetchone()
            if row and row.get("latitude") is not None and row.get("longitude") is not None:
                src = (
                    GOOGLE_CACHE_SOURCE
                    if google_source_id is not None and row.get("source_id") == google_source_id
                    else DEFAULT_GEO_SOURCE
                )
                coord = {
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "precision": "exact_number",
                    "source": src,
                }
                if cnpj:
                    await _save_company_cache_entries(
                        conn, [(cnpj, coord["latitude"], coord["longitude"], "exact_number", cep, src)]
                    )
                return coord
        except Exception as exc:
            logger.debug("geo.addresses exact number lookup skipped: %s", exc)
            await _safe_rollback(conn)

    # 2b. Exact number match via City + Street + Number (when CEP didn't match)
    if city_name and street_candidates and number and not mask_exact:
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT latitude, longitude, source_id
                    FROM geo.addresses
                    WHERE region = %(region)s
                      AND geo.immutable_unaccent(lower(city)) = geo.immutable_unaccent(lower(%(city)s))
                      AND geo.immutable_unaccent(lower(street)) = ANY(%(streets)s)
                      AND upper(trim(number)) = %(number)s
                      AND geom IS NOT NULL
                    ORDER BY COALESCE(source_id = %(google_source_id)s, FALSE) DESC, created_at DESC
                    LIMIT 1;
                    """,
                    {
                        "region": uf,
                        "city": city_name,
                        "streets": street_candidates,
                        "number": number,
                        "google_source_id": google_source_id,
                    },
                )
                row = await cur.fetchone()
            if row and row.get("latitude") is not None and row.get("longitude") is not None:
                src = (
                    GOOGLE_CACHE_SOURCE
                    if google_source_id is not None and row.get("source_id") == google_source_id
                    else DEFAULT_GEO_SOURCE
                )
                coord = {
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "precision": "exact_number",
                    "source": src,
                }
                if cnpj:
                    await _save_company_cache_entries(
                        conn, [(cnpj, coord["latitude"], coord["longitude"], "exact_number", cep, src)]
                    )
                return coord
        except Exception as exc:
            logger.debug("geo.addresses exact street+number lookup skipped: %s", exc)
            await _safe_rollback(conn)

    # 3. City + Street normalized lookup (when not LGPD-masked for MEI)
    if city_name and street_candidates and not mask_exact:
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT
                        AVG(s.latitude) AS lat,
                        AVG(s.longitude) AS lon,
                        BOOL_OR(COALESCE(s.source_id = %(google_source_id)s, FALSE)) AS has_google,
                        COUNT(*)::int AS cnt
                    FROM (
                        SELECT latitude, longitude, source_id
                        FROM geo.addresses
                        WHERE region = %(region)s
                          AND geo.immutable_unaccent(lower(city)) = geo.immutable_unaccent(lower(%(city)s))
                          AND geo.immutable_unaccent(lower(street)) = ANY(%(streets)s)
                          AND geom IS NOT NULL
                        ORDER BY COALESCE(source_id = %(google_source_id)s, FALSE) DESC, created_at DESC
                        LIMIT 50
                    ) s
                    HAVING COUNT(*) > 0;
                    """,
                    {
                        "region": uf,
                        "city": city_name,
                        "streets": street_candidates,
                        "google_source_id": google_source_id,
                    },
                )
                row = await cur.fetchone()
            if row and row.get("lat") is not None and row.get("lon") is not None:
                lat, lon = float(row["lat"]), float(row["lon"])
                src = GOOGLE_CACHE_SOURCE if row.get("has_google") else DEFAULT_GEO_SOURCE
                coord = {
                    "latitude": lat,
                    "longitude": lon,
                    "precision": "street",
                    "source": src,
                }
                if cnpj:
                    await _save_company_cache_entries(conn, [(cnpj, lat, lon, "street", cep, src)])
                return coord
        except Exception as exc:
            logger.debug("geo.addresses street lookup skipped: %s", exc)
            await _safe_rollback(conn)

    # 4. CEP centroid lookup
    if cep:
        cep_map = await _lookup_postcode_centroids_batch(conn, [(uf, cep)])
        if (uf, cep) in cep_map:
            lat, lon = cep_map[(uf, cep)]
            coord = {
                "latitude": lat,
                "longitude": lon,
                "precision": "cep_centroid",
                "source": DEFAULT_GEO_SOURCE,
            }
            if cnpj and not mask_exact:
                await _save_company_cache_entries(conn, [(cnpj, lat, lon, "cep_centroid", cep, DEFAULT_GEO_SOURCE)])
            return coord

    # 5. City centroid fallback
    if city_name:
        city_map = await _lookup_city_centroids_batch(conn, [(uf, city_name)])
        if (uf, city_name) in city_map:
            lat, lon = city_map[(uf, city_name)]
            coord = {
                "latitude": lat,
                "longitude": lon,
                "precision": "city_centroid",
                "source": DEFAULT_GEO_SOURCE,
            }
            if cnpj and not mask_exact:
                await _save_company_cache_entries(conn, [(cnpj, lat, lon, "city_centroid", cep, DEFAULT_GEO_SOURCE)])
            return coord

    return None


async def resolve_centroids_for_groups(
    conn: psycopg.AsyncConnection,
    cep_pairs: Sequence[Tuple[str, str]],
    city_pairs: Sequence[Tuple[str, str]],
) -> Tuple[Dict[Tuple[str, str], Tuple[float, float]], Dict[Tuple[str, str], Tuple[float, float]]]:
    """Resolve CEP centroids and fallback city centroids for aggregated map clusters."""
    cep_centroids = await _lookup_postcode_centroids_batch(conn, cep_pairs)
    city_centroids = await _lookup_city_centroids_batch(conn, city_pairs)
    return cep_centroids, city_centroids


async def lookup_cep_prefix_bounds(
    conn: psycopg.AsyncConnection,
    prefix_items: Sequence[Tuple[str, str]],
) -> Dict[Tuple[str, str], Dict[str, float]]:
    """Aggregate min/max/avg coordinates from geo_postcode_cache for each (UF, CEP prefix)."""
    unique_items = sorted(
        {
            (uf, prefix)
            for uf, prefix in prefix_items
            if uf in BRAZIL_STATES and prefix and prefix.isdigit() and 1 <= len(prefix) <= 8
        }
    )
    if not unique_items:
        return {}

    regions = [item[0] for item in unique_items]
    prefixes = [item[1] for item in unique_items]
    starts = [item[1].ljust(8, "0") for item in unique_items]
    ends = [item[1].ljust(8, "9") for item in unique_items]

    result: Dict[Tuple[str, str], Dict[str, float]] = {}
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    p.region,
                    p.prefix,
                    MIN(c.latitude) AS min_lat,
                    MAX(c.latitude) AS max_lat,
                    MIN(c.longitude) AS min_lon,
                    MAX(c.longitude) AS max_lon,
                    AVG(c.latitude) AS avg_lat,
                    AVG(c.longitude) AS avg_lon
                FROM unnest(%(regions)s::text[], %(prefixes)s::text[], %(starts)s::text[], %(ends)s::text[]) AS p(region, prefix, p_start, p_end)
                JOIN app_saas.geo_postcode_cache c
                  ON c.region = p.region
                 AND c.postcode_digits >= p.p_start
                 AND c.postcode_digits <= p.p_end
                GROUP BY p.region, p.prefix;
                """,
                {
                    "regions": regions,
                    "prefixes": prefixes,
                    "starts": starts,
                    "ends": ends,
                },
            )
            for r in await cur.fetchall():
                if r.get("avg_lat") is None or r.get("avg_lon") is None:
                    continue
                result[(r["region"], r["prefix"])] = {
                    "min_lat": float(r["min_lat"]),
                    "max_lat": float(r["max_lat"]),
                    "min_lon": float(r["min_lon"]),
                    "max_lon": float(r["max_lon"]),
                    "avg_lat": float(r["avg_lat"]),
                    "avg_lon": float(r["avg_lon"]),
                }
    except Exception as exc:
        logger.debug("geo_postcode_cache prefix bounds lookup skipped: %s", exc)
        await _safe_rollback(conn)
    return result


async def _resolve_uncached_geo_entries(
    conn: psycopg.AsyncConnection,
    unmasked_entries: Sequence[dict],
    masked_entries: Sequence[dict],
    *,
    key_field: str = "cnpj",
    mask_mei_exact: bool = True,
) -> Tuple[Dict[str, dict], List[Tuple[str, float, float, str, Optional[str], str]]]:
    """Resolve coordinates via 5-tier geo lookup for uncached establishments or address groups."""
    resolved: Dict[str, dict] = {}
    to_cache: List[Tuple[str, float, float, str, Optional[str], str]] = []
    still_missing: List[dict] = list(masked_entries)

    if unmasked_entries:
        google_source_id = await get_google_source_id(conn, create_if_missing=False)

        # 1. Batch exact_number by (UF, CEP, number)
        cep_num_triples: List[Tuple[str, str, str]] = []
        for est in unmasked_entries:
            uf = clean_state(est.get("state"))
            cep = clean_postal_code(est.get("postal_code"))
            num = clean_street_number(est.get("number"))
            if uf and cep and num:
                cep_num_triples.append((uf, cep, num))
        exact_cep_map = await _lookup_exact_number_by_cep_batch(conn, cep_num_triples, google_source_id)

        after_exact_cep: List[dict] = []
        for est in unmasked_entries:
            entry_key = str(est.get(key_field) or "").strip()
            uf = clean_state(est.get("state"))
            cep = clean_postal_code(est.get("postal_code"))
            num = clean_street_number(est.get("number"))
            if uf and cep and num and (uf, cep, num) in exact_cep_map:
                lat, lon, src = exact_cep_map[(uf, cep, num)]
                resolved[entry_key] = {
                    "latitude": lat,
                    "longitude": lon,
                    "precision": "exact_number",
                    "source": src,
                }
                to_cache.append((entry_key, lat, lon, "exact_number", cep, src))
            else:
                after_exact_cep.append(est)

        # 2. Batch exact_number by (UF, city, street, number)
        street_num_queries: List[Tuple[str, str, List[str], str]] = []
        for est in after_exact_cep:
            uf = clean_state(est.get("state"))
            city = (est.get("municipality_name") or "").strip()
            num = clean_street_number(est.get("number"))
            cands = build_street_candidates(est.get("street_type"), est.get("street"))
            if uf and city and cands and num:
                street_num_queries.append((uf, city, cands, num))
        exact_street_map = await _lookup_exact_number_by_street_batch(conn, street_num_queries, google_source_id)

        after_exact_street: List[dict] = []
        for est in after_exact_cep:
            entry_key = str(est.get(key_field) or "").strip()
            uf = clean_state(est.get("state"))
            cep = clean_postal_code(est.get("postal_code"))
            city = (est.get("municipality_name") or "").strip()
            num = clean_street_number(est.get("number"))
            cands = build_street_candidates(est.get("street_type"), est.get("street"))
            key = (uf, city, cands[0], num) if uf and city and cands and num else None
            if key and key in exact_street_map:
                lat, lon, src = exact_street_map[key]
                resolved[entry_key] = {
                    "latitude": lat,
                    "longitude": lon,
                    "precision": "exact_number",
                    "source": src,
                }
                to_cache.append((entry_key, lat, lon, "exact_number", cep, src))
            else:
                after_exact_street.append(est)

        # 3. Batch street-level by (UF, city, street)
        street_queries: List[Tuple[str, str, List[str]]] = []
        for est in after_exact_street:
            uf = clean_state(est.get("state"))
            city = (est.get("municipality_name") or "").strip()
            cands = build_street_candidates(est.get("street_type"), est.get("street"))
            if uf and city and cands:
                street_queries.append((uf, city, cands))
        street_map = await _lookup_street_centroids_batch(conn, street_queries, google_source_id)

        for est in after_exact_street:
            entry_key = str(est.get(key_field) or "").strip()
            uf = clean_state(est.get("state"))
            cep = clean_postal_code(est.get("postal_code"))
            city = (est.get("municipality_name") or "").strip()
            cands = build_street_candidates(est.get("street_type"), est.get("street"))
            key = (uf, city, cands[0]) if uf and city and cands else None
            if key and key in street_map:
                lat, lon, src = street_map[key]
                resolved[entry_key] = {
                    "latitude": lat,
                    "longitude": lon,
                    "precision": "street",
                    "source": src,
                }
                to_cache.append((entry_key, lat, lon, "street", cep, src))
            else:
                still_missing.append(est)

    # 4. CEP centroid fallback (for remaining unmasked + LGPD-masked MEIs)
    if still_missing:
        cep_pairs: List[Tuple[str, str]] = []
        for est in still_missing:
            uf = clean_state(est.get("state"))
            cep = clean_postal_code(est.get("postal_code"))
            if uf and cep:
                cep_pairs.append((uf, cep))

        cep_centroids = await _lookup_postcode_centroids_batch(conn, cep_pairs)
        after_cep: List[dict] = []

        for est in still_missing:
            entry_key = str(est.get(key_field) or "").strip()
            uf = clean_state(est.get("state"))
            cep = clean_postal_code(est.get("postal_code"))
            is_masked = mask_mei_exact and bool(est.get("mei_optant"))
            if uf and cep and (uf, cep) in cep_centroids:
                lat, lon = cep_centroids[(uf, cep)]
                resolved[entry_key] = {
                    "latitude": lat,
                    "longitude": lon,
                    "precision": "cep_centroid",
                    "source": DEFAULT_GEO_SOURCE,
                }
                if not is_masked:
                    to_cache.append((entry_key, lat, lon, "cep_centroid", cep, DEFAULT_GEO_SOURCE))
            else:
                after_cep.append(est)

        # 5. City centroid fallback
        if after_cep:
            city_pairs: List[Tuple[str, str]] = []
            for est in after_cep:
                uf = clean_state(est.get("state"))
                city = (est.get("municipality_name") or "").strip()
                if uf and city:
                    city_pairs.append((uf, city))
            city_centroids = await _lookup_city_centroids_batch(conn, city_pairs)

            for est in after_cep:
                entry_key = str(est.get(key_field) or "").strip()
                uf = clean_state(est.get("state"))
                city = (est.get("municipality_name") or "").strip()
                cep = clean_postal_code(est.get("postal_code"))
                is_masked = mask_mei_exact and bool(est.get("mei_optant"))
                if uf and city and (uf, city) in city_centroids:
                    lat, lon = city_centroids[(uf, city)]
                    resolved[entry_key] = {
                        "latitude": lat,
                        "longitude": lon,
                        "precision": "city_centroid",
                        "source": DEFAULT_GEO_SOURCE,
                    }
                    if not is_masked:
                        to_cache.append((entry_key, lat, lon, "city_centroid", cep, DEFAULT_GEO_SOURCE))

    return resolved, to_cache


async def resolve_address_groups_geo_batch(
    conn: psycopg.AsyncConnection,
    address_groups: Sequence[dict],
    *,
    mask_mei_exact: bool = True,
) -> Dict[str, dict]:
    """Resolve coordinates for deduplicated address groups keyed by group_id."""
    if not address_groups:
        return {}
    unmasked: List[dict] = []
    masked: List[dict] = []
    for grp in address_groups:
        if mask_mei_exact and bool(grp.get("mei_optant")):
            masked.append(grp)
        else:
            unmasked.append(grp)
    resolved, _ = await _resolve_uncached_geo_entries(
        conn,
        unmasked,
        masked,
        key_field="group_id",
        mask_mei_exact=mask_mei_exact,
    )
    return resolved


async def resolve_establishments_geo_batch(
    conn: psycopg.AsyncConnection,
    establishments: Sequence[dict],
    *,
    mask_mei_exact: bool = True,
) -> Dict[str, dict]:
    """Batch-resolve georeferenced coordinates for establishments with exact/street priority."""
    if not establishments:
        return {}

    cnpjs = [str(e.get("cnpj") or "").strip() for e in establishments if e.get("cnpj")]
    cached_by_cnpj = await _lookup_company_cache(conn, cnpjs)

    resolved: Dict[str, dict] = {}
    unmasked_unresolved: List[dict] = []
    masked_unresolved: List[dict] = []

    for est in establishments:
        cnpj = str(est.get("cnpj") or "").strip()
        if not cnpj:
            continue
        is_mei = bool(est.get("mei_optant"))
        is_masked = mask_mei_exact and is_mei
        cached = cached_by_cnpj.get(cnpj)
        if cached and not (is_masked and cached["precision"] in ("exact_number", "street")):
            resolved[cnpj] = cached
        elif is_masked:
            masked_unresolved.append(est)
        else:
            unmasked_unresolved.append(est)

    if not unmasked_unresolved and not masked_unresolved:
        return resolved

    newly_resolved, to_cache = await _resolve_uncached_geo_entries(
        conn,
        unmasked_unresolved,
        masked_unresolved,
        key_field="cnpj",
        mask_mei_exact=mask_mei_exact,
    )
    resolved.update(newly_resolved)

    if to_cache:
        await _save_company_cache_entries(conn, to_cache)

    return resolved


async def _map_city_names_to_municipality_codes(
    conn: psycopg.AsyncConnection, region_cities: Iterable[Tuple[str, str]]
) -> List[str]:
    """Translate (UF, city name) pairs from geo.addresses into 4-digit RFB municipality codes."""
    by_state: Dict[str, set] = {}
    for uf, city in region_cities:
        norm = normalize_place_name(city) if city else ""
        if uf in BRAZIL_STATES and norm:
            by_state.setdefault(uf, set()).add(norm)

    if not by_state:
        return []

    codes: List[str] = []
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT DISTINCT state, municipality_code, municipality_name
                FROM location_hierarchy
                WHERE state = ANY(%(states)s);
                """,
                {"states": sorted(by_state.keys())},
            )
            rows = await cur.fetchall()
        for r in rows:
            st = (r.get("state") or "").strip().upper()
            m_name = normalize_place_name(r.get("municipality_name") or "")
            if m_name and m_name in by_state.get(st, ()):
                codes.append(r["municipality_code"])
    except Exception as exc:
        logger.debug("municipality code mapping skipped: %s", exc)
        await _safe_rollback(conn)
    return sorted(set(codes))


async def resolve_spatial_filter_targets(
    conn: psycopg.AsyncConnection,
    *,
    geo_radius: Optional[Any] = None,
    geo_bounds: Optional[Any] = None,
    viewport_bounds: Optional[Any] = None,
    max_ceps: int = 15000,
) -> dict:
    """Resolve a map circle or bounding box into indexed UF, municipality_code, and CEP lists."""
    if not geo_radius and not geo_bounds and not viewport_bounds:
        return {"active": False}

    def _normalize_box(box: Any) -> Tuple[float, float, float, float]:
        b_min_lat = max(-90.0, min(float(box.south), float(box.north)))
        b_max_lat = min(90.0, max(float(box.south), float(box.north)))
        b_min_lon = max(-180.0, min(float(box.west), float(box.east)))
        b_max_lon = min(180.0, max(float(box.west), float(box.east)))
        return b_min_lat, b_max_lat, b_min_lon, b_max_lon

    if geo_radius is not None:
        lat = float(geo_radius.latitude)
        lon = float(geo_radius.longitude)
        radius_km = min(max(float(geo_radius.radius_km), 0.2), 50.0)
        lat_deg = radius_km / 111.32
        lon_deg = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.2))
        min_lat, max_lat = max(-90.0, lat - lat_deg), min(90.0, lat + lat_deg)
        min_lon, max_lon = max(-180.0, lon - lon_deg), min(180.0, lon + lon_deg)
        center_lat, center_lon = lat, lon
        radius_m = radius_km * 1000.0
        if geo_bounds is not None:
            gb_min_lat, gb_max_lat, gb_min_lon, gb_max_lon = _normalize_box(geo_bounds)
            min_lat = max(min_lat, gb_min_lat)
            max_lat = min(max_lat, gb_max_lat)
            min_lon = max(min_lon, gb_min_lon)
            max_lon = min(max_lon, gb_max_lon)
        if viewport_bounds is not None:
            vp_min_lat, vp_max_lat, vp_min_lon, vp_max_lon = _normalize_box(viewport_bounds)
            min_lat = max(min_lat, vp_min_lat)
            max_lat = min(max_lat, vp_max_lat)
            min_lon = max(min_lon, vp_min_lon)
            max_lon = min(max_lon, vp_max_lon)
    else:
        active_box = geo_bounds or viewport_bounds
        min_lat, max_lat, min_lon, max_lon = _normalize_box(active_box)
        if geo_bounds is not None and viewport_bounds is not None:
            vp_min_lat, vp_max_lat, vp_min_lon, vp_max_lon = _normalize_box(viewport_bounds)
            min_lat = max(min_lat, vp_min_lat)
            max_lat = min(max_lat, vp_max_lat)
            min_lon = max(min_lon, vp_min_lon)
            max_lon = min(max_lon, vp_max_lon)
        center_lat = (min_lat + max_lat) / 2.0
        center_lon = (min_lon + max_lon) / 2.0
        radius_m = None

    if min_lat > max_lat or min_lon > max_lon:
        return {"active": True, "empty": True}

    envelope_params = {
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "center_lon": center_lon,
        "center_lat": center_lat,
        "max_ceps": max_ceps,
    }

    # 1. Detect intersecting Brazilian states (partitions) quickly via GiST index
    regions: List[str] = []
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT DISTINCT region
                FROM (
                    SELECT region
                    FROM geo.addresses
                    WHERE geom IS NOT NULL
                      AND geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)
                    LIMIT 300
                ) sub
                WHERE region = ANY(%(valid_states)s);
                """,
                {**envelope_params, "valid_states": sorted(BRAZIL_STATES)},
            )
            regions = [r["region"] for r in await cur.fetchall() if r.get("region") in BRAZIL_STATES]
    except Exception as exc:
        logger.debug("Spatial region probe failed: %s", exc)
        await _safe_rollback(conn)
        return {"active": True, "empty": True}

    if not regions:
        return {"active": True, "empty": True}

    # 2. Fetch distinct CEPs inside the bounding box / circle, prioritizing proximity to center
    ceps: List[str] = []
    city_pairs: List[Tuple[str, str]] = []
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            if radius_m is not None:
                envelope_params["radius_m"] = radius_m
                envelope_params["regions"] = regions
                await cur.execute(
                    """
                    SELECT
                        postcode_digits,
                        MIN(
                            ST_Distance(
                                geom::geography,
                                ST_SetSRID(ST_MakePoint(%(center_lon)s, %(center_lat)s), 4326)::geography
                            )
                        ) AS dist_m
                    FROM (
                        SELECT postcode_digits, geom
                        FROM geo.addresses
                        WHERE region = ANY(%(regions)s)
                          AND geom IS NOT NULL
                          AND postcode_digits IS NOT NULL
                          AND geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)
                        LIMIT 100000
                    ) sub
                    WHERE ST_DWithin(
                        geom::geography,
                        ST_SetSRID(ST_MakePoint(%(center_lon)s, %(center_lat)s), 4326)::geography,
                        %(radius_m)s
                    )
                    GROUP BY postcode_digits
                    ORDER BY dist_m ASC
                    LIMIT %(max_ceps)s;
                    """,
                    envelope_params,
                )
            else:
                envelope_params["regions"] = regions
                await cur.execute(
                    """
                    SELECT
                        postcode_digits,
                        MIN(
                            ST_Distance(
                                geom,
                                ST_SetSRID(ST_MakePoint(%(center_lon)s, %(center_lat)s), 4326)
                            )
                        ) AS dist_deg
                    FROM (
                        SELECT postcode_digits, geom
                        FROM geo.addresses
                        WHERE region = ANY(%(regions)s)
                          AND geom IS NOT NULL
                          AND postcode_digits IS NOT NULL
                          AND geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)
                        LIMIT 100000
                    ) sub
                    GROUP BY postcode_digits
                    ORDER BY dist_deg ASC
                    LIMIT %(max_ceps)s;
                    """,
                    envelope_params,
                )
            ceps = [r["postcode_digits"] for r in await cur.fetchall() if r.get("postcode_digits")]

            # 3. Also sample non-null city names inside the area to narrow company_search_index via municipality_code
            await cur.execute(
                """
                SELECT DISTINCT region, city
                FROM (
                    SELECT region, city
                    FROM geo.addresses
                    WHERE region = ANY(%(regions)s)
                      AND city IS NOT NULL
                      AND geom IS NOT NULL
                      AND geom && ST_MakeEnvelope(%(min_lon)s, %(min_lat)s, %(max_lon)s, %(max_lat)s, 4326)
                    LIMIT 1000
                ) sub;
                """,
                envelope_params,
            )
            city_pairs = [(r["region"], r["city"]) for r in await cur.fetchall() if r.get("city")]
    except Exception as exc:
        logger.debug("Spatial CEP probe failed: %s", exc)
        await _safe_rollback(conn)
        return {"active": True, "empty": True}

    if not ceps:
        return {"active": True, "empty": True}

    municipality_codes = await _map_city_names_to_municipality_codes(conn, city_pairs)

    return {
        "active": True,
        "empty": False,
        "states": sorted(set(regions)),
        "municipality_codes": municipality_codes,
        "postal_codes": ceps,
    }

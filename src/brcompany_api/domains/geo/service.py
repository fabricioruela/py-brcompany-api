"""Georeferencing service bridging RFB establishments (registry) with br_company.geo (geo.addresses)."""

import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
import psycopg
from psycopg.rows import dict_row

from brcompany_api.utils.geo import BRAZIL_STATES, normalize_place_name

logger = logging.getLogger(__name__)

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


async def _lookup_company_cache(
    conn: psycopg.AsyncConnection, cnpjs: Sequence[str]
) -> Dict[str, dict]:
    if not cnpjs:
        return {}
    try:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT cnpj, latitude, longitude, precision
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
                "source": "br_company.geo",
            }
            for r in rows
            if r.get("latitude") is not None and r.get("longitude") is not None
        }
    except Exception as exc:
        logger.debug("company_geo_cache lookup skipped: %s", exc)
        await _safe_rollback(conn)
        return {}


async def _save_company_cache_entries(
    conn: psycopg.AsyncConnection, entries: List[Tuple[str, float, float, str, Optional[str]]]
) -> None:
    if not entries:
        return
    try:
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO app_saas.company_geo_cache (cnpj, latitude, longitude, precision, postal_code)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (cnpj) DO NOTHING;
                """,
                entries,
            )
        await conn.commit()
    except Exception as exc:
        logger.debug("company_geo_cache insert skipped: %s", exc)
        await _safe_rollback(conn)


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

    # 1. Check company_geo_cache
    if cnpj:
        cached = (await _lookup_company_cache(conn, [cnpj])).get(cnpj)
        if cached and (not mask_exact or cached["precision"] in ("cep_centroid", "city_centroid")):
            return cached

    # 2. Exact number match via CEP (only when not LGPD-masked for MEI)
    if cep and number and not mask_exact:
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT latitude, longitude
                    FROM geo.addresses
                    WHERE region = %(region)s
                      AND postcode_digits = %(cep)s
                      AND upper(trim(number)) = %(number)s
                      AND geom IS NOT NULL
                    LIMIT 1;
                    """,
                    {"region": uf, "cep": cep, "number": number},
                )
                row = await cur.fetchone()
            if row and row.get("latitude") is not None and row.get("longitude") is not None:
                coord = {
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "precision": "exact_number",
                    "source": "br_company.geo",
                }
                if cnpj:
                    await _save_company_cache_entries(
                        conn, [(cnpj, coord["latitude"], coord["longitude"], "exact_number", cep)]
                    )
                return coord
        except Exception as exc:
            logger.debug("geo.addresses exact number lookup skipped: %s", exc)
            await _safe_rollback(conn)

    # 3. CEP centroid lookup
    if cep:
        cep_map = await _lookup_postcode_centroids_batch(conn, [(uf, cep)])
        if (uf, cep) in cep_map:
            lat, lon = cep_map[(uf, cep)]
            coord = {
                "latitude": lat,
                "longitude": lon,
                "precision": "cep_centroid",
                "source": "br_company.geo",
            }
            if cnpj:
                await _save_company_cache_entries(conn, [(cnpj, lat, lon, "cep_centroid", cep)])
            return coord

    # 4. City + Street normalized lookup (when not LGPD-masked for MEI)
    street_candidates = build_street_candidates(est.get("street_type"), est.get("street"))
    if city_name and street_candidates and not mask_exact:
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT AVG(s.latitude) AS lat, AVG(s.longitude) AS lon, COUNT(*)::int AS cnt
                    FROM (
                        SELECT latitude, longitude
                        FROM geo.addresses
                        WHERE region = %(region)s
                          AND geo.immutable_unaccent(lower(city)) = geo.immutable_unaccent(lower(%(city)s))
                          AND geo.immutable_unaccent(lower(street)) = ANY(%(streets)s)
                          AND geom IS NOT NULL
                        LIMIT 50
                    ) s
                    HAVING COUNT(*) > 0;
                    """,
                    {"region": uf, "city": city_name, "streets": street_candidates},
                )
                row = await cur.fetchone()
            if row and row.get("lat") is not None and row.get("lon") is not None:
                lat, lon = float(row["lat"]), float(row["lon"])
                coord = {
                    "latitude": lat,
                    "longitude": lon,
                    "precision": "street",
                    "source": "br_company.geo",
                }
                if cnpj:
                    await _save_company_cache_entries(conn, [(cnpj, lat, lon, "street", cep)])
                return coord
        except Exception as exc:
            logger.debug("geo.addresses street lookup skipped: %s", exc)
            await _safe_rollback(conn)

    # 5. City centroid fallback
    if city_name:
        city_map = await _lookup_city_centroids_batch(conn, [(uf, city_name)])
        if (uf, city_name) in city_map:
            lat, lon = city_map[(uf, city_name)]
            coord = {
                "latitude": lat,
                "longitude": lon,
                "precision": "city_centroid",
                "source": "br_company.geo",
            }
            if cnpj:
                await _save_company_cache_entries(conn, [(cnpj, lat, lon, "city_centroid", cep)])
            return coord

    return None


async def resolve_establishments_geo_batch(
    conn: psycopg.AsyncConnection,
    establishments: Sequence[dict],
    *,
    mask_mei_exact: bool = True,
) -> Dict[str, dict]:
    """Batch-resolve georeferenced coordinates for up to ~200 establishments."""
    if not establishments:
        return {}

    cnpjs = [str(e.get("cnpj") or "").strip() for e in establishments if e.get("cnpj")]
    cached_by_cnpj = await _lookup_company_cache(conn, cnpjs)

    resolved: Dict[str, dict] = {}
    unresolved: List[dict] = []

    for est in establishments:
        cnpj = str(est.get("cnpj") or "").strip()
        if not cnpj:
            continue
        is_mei = bool(est.get("mei_optant"))
        cached = cached_by_cnpj.get(cnpj)
        if cached and not (mask_mei_exact and is_mei and cached["precision"] == "exact_number"):
            resolved[cnpj] = cached
        else:
            unresolved.append(est)

    if not unresolved:
        return resolved

    cep_pairs: List[Tuple[str, str]] = []
    for est in unresolved:
        uf = clean_state(est.get("state"))
        cep = clean_postal_code(est.get("postal_code"))
        if uf and cep:
            cep_pairs.append((uf, cep))

    cep_centroids = await _lookup_postcode_centroids_batch(conn, cep_pairs)

    still_missing: List[dict] = []
    to_cache: List[Tuple[str, float, float, str, Optional[str]]] = []

    for est in unresolved:
        cnpj = str(est.get("cnpj") or "").strip()
        uf = clean_state(est.get("state"))
        cep = clean_postal_code(est.get("postal_code"))
        if uf and cep and (uf, cep) in cep_centroids:
            lat, lon = cep_centroids[(uf, cep)]
            resolved[cnpj] = {
                "latitude": lat,
                "longitude": lon,
                "precision": "cep_centroid",
                "source": "br_company.geo",
            }
            to_cache.append((cnpj, lat, lon, "cep_centroid", cep))
        else:
            still_missing.append(est)

    if still_missing:
        city_pairs: List[Tuple[str, str]] = []
        for est in still_missing:
            uf = clean_state(est.get("state"))
            city = (est.get("municipality_name") or "").strip()
            if uf and city:
                city_pairs.append((uf, city))
        city_centroids = await _lookup_city_centroids_batch(conn, city_pairs)

        for est in still_missing:
            cnpj = str(est.get("cnpj") or "").strip()
            uf = clean_state(est.get("state"))
            city = (est.get("municipality_name") or "").strip()
            cep = clean_postal_code(est.get("postal_code"))
            if uf and city and (uf, city) in city_centroids:
                lat, lon = city_centroids[(uf, city)]
                resolved[cnpj] = {
                    "latitude": lat,
                    "longitude": lon,
                    "precision": "city_centroid",
                    "source": "br_company.geo",
                }
                to_cache.append((cnpj, lat, lon, "city_centroid", cep))

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
    max_ceps: int = 600,
) -> dict:
    """Resolve a map circle or bounding box into indexed UF, municipality_code, and CEP lists."""
    if not geo_radius and not geo_bounds:
        return {"active": False}

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
    else:
        min_lat = max(-90.0, min(float(geo_bounds.south), float(geo_bounds.north)))
        max_lat = min(90.0, max(float(geo_bounds.south), float(geo_bounds.north)))
        min_lon = max(-180.0, min(float(geo_bounds.west), float(geo_bounds.east)))
        max_lon = min(180.0, max(float(geo_bounds.west), float(geo_bounds.east)))
        center_lat = (min_lat + max_lat) / 2.0
        center_lon = (min_lon + max_lon) / 2.0
        radius_m = None

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
                        LIMIT 25000
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
                        LIMIT 25000
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

"""Google Geocoding API client and persistence helpers for admin-triggered georeferencing."""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence
import httpx
import psycopg
from psycopg.rows import dict_row

from brcompany_api.domains.geo import service as geo_service

logger = logging.getLogger(__name__)

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

_LOCATION_TYPE_PRECISION = {
    "ROOFTOP": "exact_number",
    "RANGE_INTERPOLATED": "exact_number",
    "GEOMETRIC_CENTER": "street",
}


def build_full_street(street_type: Optional[str], street: Optional[str]) -> str:
    """Combine RFB street_type and street without duplicating the prefix."""
    st_name = " ".join((street or "").strip().split())
    if not st_name:
        return ""
    st_type = " ".join((street_type or "").strip().split())
    if not st_type:
        return st_name
    if st_name.upper().startswith(f"{st_type.upper()} "):
        return st_name
    return f"{st_type} {st_name}"


def build_google_geocoding_query(est: dict) -> Optional[Dict[str, Any]]:
    """Build structured address query from logradouro, number, municipality, and UF."""
    uf = geo_service.clean_state(est.get("state"))
    city = " ".join((est.get("municipality_name") or "").strip().split())
    full_street = build_full_street(est.get("street_type"), est.get("street"))
    number = geo_service.clean_street_number(est.get("number"))

    if not uf or not city or not full_street:
        return None

    if number:
        query_address = f"{full_street}, {number}, {city} - {uf}, Brasil"
    else:
        query_address = f"{full_street}, {city} - {uf}, Brasil"

    return {
        "uf": uf,
        "city": city,
        "full_street": full_street,
        "number": number,
        "query_address": query_address,
        "components": f"country:BR|administrative_area:{uf}",
    }


def map_google_location_type(location_type: Optional[str]) -> Optional[str]:
    """Map Google Geocoding location_type to internal geo_precision, rejecting APPROXIMATE."""
    if not location_type:
        return None
    return _LOCATION_TYPE_PRECISION.get(location_type.strip().upper())


def _is_within_brazil_bounds(lat: float, lon: float) -> bool:
    return -35.0 <= lat <= 6.0 and -75.0 <= lon <= -32.0


async def geocode_address_google(
    client: httpx.AsyncClient,
    api_key: str,
    query_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Call Google Geocoding REST API for a prepared address query."""
    params = {
        "address": query_info["query_address"],
        "components": query_info["components"],
        "region": "br",
        "language": "pt-BR",
        "key": api_key,
    }
    try:
        response = await client.get(GOOGLE_GEOCODE_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("Google Geocoding HTTP error for '%s': %s", query_info["query_address"], exc)
        return {
            "status": "error",
            "query_address": query_info["query_address"],
            "message": f"Erro ao consultar Google Geocoding API: {exc}",
        }

    api_status = str(payload.get("status") or "").upper()
    if api_status == "ZERO_RESULTS":
        return {
            "status": "not_found",
            "query_address": query_info["query_address"],
            "message": "Endereço não localizado pela Google Geocoding API.",
        }

    if api_status != "OK":
        detail = payload.get("error_message") or api_status or "UNKNOWN_ERROR"
        logger.warning("Google Geocoding status=%s detail=%s", api_status, detail)
        return {
            "status": "error",
            "query_address": query_info["query_address"],
            "message": f"Google Geocoding retornou {api_status}: {detail}",
        }

    results = payload.get("results") or []
    if not results:
        return {
            "status": "not_found",
            "query_address": query_info["query_address"],
            "message": "Nenhum resultado retornado pela Google Geocoding API.",
        }

    top = results[0]
    geometry = top.get("geometry") or {}
    location = geometry.get("location") or {}
    location_type = geometry.get("location_type")
    formatted_address = top.get("formatted_address")

    lat = location.get("lat")
    lng = location.get("lng")
    if lat is None or lng is None:
        return {
            "status": "not_found",
            "query_address": query_info["query_address"],
            "message": "Resultado do Google sem coordenadas.",
        }

    lat_f = float(lat)
    lng_f = float(lng)
    if not _is_within_brazil_bounds(lat_f, lng_f):
        return {
            "status": "skipped_low_precision",
            "query_address": query_info["query_address"],
            "google_location_type": location_type,
            "formatted_address": formatted_address,
            "message": "Coordenada retornada fora dos limites territoriais do Brasil.",
        }

    mapped_precision = map_google_location_type(location_type)
    if not mapped_precision:
        return {
            "status": "skipped_low_precision",
            "query_address": query_info["query_address"],
            "google_location_type": location_type,
            "formatted_address": formatted_address,
            "message": f"Precisão '{location_type or 'APPROXIMATE'}' ignorada para não degradar marcadores.",
        }

    return {
        "status": "updated",
        "latitude": lat_f,
        "longitude": lng_f,
        "precision": mapped_precision,
        "source": geo_service.GOOGLE_CACHE_SOURCE,
        "google_location_type": location_type,
        "formatted_address": formatted_address,
        "query_address": query_info["query_address"],
    }


async def _fetch_establishments_for_geocoding(
    conn: psycopg.AsyncConnection, cnpjs: Sequence[str]
) -> Dict[str, dict]:
    if not cnpjs:
        return {}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                e.cnpj,
                e.street_type,
                e.street,
                e.number,
                e.neighborhood,
                e.postal_code,
                e.state,
                m.description AS municipality_name
            FROM establishments e
            LEFT JOIN municipalities m ON m.code = e.municipality_code
            WHERE e.cnpj = ANY(%(cnpjs)s);
            """,
            {"cnpjs": list(cnpjs)},
        )
        rows = await cur.fetchall()
    return {str(r["cnpj"]).strip(): dict(r) for r in rows if r.get("cnpj")}


async def persist_google_geocode_result(
    conn: psycopg.AsyncConnection,
    est: dict,
    query_info: Dict[str, Any],
    geo_result: Dict[str, Any],
) -> None:
    """Persist Google Geocoding coordinate in geo.addresses and app_saas.company_geo_cache."""
    cnpj = str(est["cnpj"]).strip()
    uf = query_info["uf"]
    city = query_info["city"]
    full_street = query_info["full_street"]
    number = query_info.get("number")
    cep = geo_service.clean_postal_code(est.get("postal_code"))
    district = " ".join((est.get("neighborhood") or "").strip().split()) or None
    lat = float(geo_result["latitude"])
    lon = float(geo_result["longitude"])
    precision = str(geo_result["precision"])
    accuracy = str(geo_result.get("google_location_type") or precision)

    source_id = await geo_service.get_google_source_id(conn, create_if_missing=True)
    if source_id is not None:
        try:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM geo.addresses
                    WHERE source_id = %(source_id)s
                      AND region = %(region)s
                      AND geo.immutable_unaccent(lower(city)) = geo.immutable_unaccent(lower(%(city)s))
                      AND geo.immutable_unaccent(lower(street)) = geo.immutable_unaccent(lower(%(street)s))
                      AND COALESCE(upper(trim(number)), '') = COALESCE(%(number)s, '')
                    LIMIT 1;
                    """,
                    {
                        "source_id": source_id,
                        "region": uf,
                        "city": city,
                        "street": full_street,
                        "number": number,
                    },
                )
                existing = await cur.fetchone()
                if existing:
                    await cur.execute(
                        """
                        UPDATE geo.addresses
                        SET latitude = %(lat)s,
                            longitude = %(lon)s,
                            postcode_digits = COALESCE(%(cep)s, postcode_digits),
                            postcode = COALESCE(%(cep)s, postcode),
                            district = COALESCE(%(district)s, district),
                            accuracy = %(accuracy)s,
                            created_at = now()
                        WHERE region = %(region)s AND id = %(id)s;
                        """,
                        {
                            "lat": lat,
                            "lon": lon,
                            "cep": cep,
                            "district": district,
                            "accuracy": accuracy,
                            "region": uf,
                            "id": existing["id"],
                        },
                    )
                else:
                    await cur.execute(
                        """
                        INSERT INTO geo.addresses (
                            source_id, latitude, longitude, region,
                            postcode_digits, postcode, number, street, district, city, accuracy, oa_id
                        )
                        VALUES (
                            %(source_id)s, %(lat)s, %(lon)s, %(region)s,
                            %(cep)s, %(cep)s, %(number)s, %(street)s, %(district)s, %(city)s, %(accuracy)s, %(oa_id)s
                        );
                        """,
                        {
                            "source_id": source_id,
                            "lat": lat,
                            "lon": lon,
                            "region": uf,
                            "cep": cep,
                            "number": number,
                            "street": full_street,
                            "district": district,
                            "city": city,
                            "accuracy": accuracy,
                            "oa_id": f"google:{cnpj}",
                        },
                    )
        except Exception as exc:
            logger.warning("Failed to upsert geo.addresses for CNPJ %s: %s", cnpj, exc)
            await geo_service._safe_rollback(conn)
            raise

    await geo_service.upsert_company_cache_entry(
        conn,
        cnpj=cnpj,
        latitude=lat,
        longitude=lon,
        precision=precision,
        postal_code=cep,
        source=geo_service.GOOGLE_CACHE_SOURCE,
        commit=False,
    )


async def admin_geocode_companies(
    conn: psycopg.AsyncConnection,
    cnpjs: Sequence[str],
    *,
    api_key: str,
    timeout_seconds: float = 10.0,
    max_concurrency: int = 5,
) -> List[Dict[str, Any]]:
    """Force Google Geocoding for a batch of CNPJs and update geo.addresses + company_geo_cache."""
    unique_cnpjs: List[str] = []
    seen = set()
    for raw in cnpjs:
        clean = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(clean) == 14 and clean not in seen:
            seen.add(clean)
            unique_cnpjs.append(clean)

    if not unique_cnpjs:
        return []

    est_by_cnpj = await _fetch_establishments_for_geocoding(conn, unique_cnpjs)
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _run_single(client: httpx.AsyncClient, cnpj: str):
        est = est_by_cnpj.get(cnpj)
        if not est:
            return cnpj, None, None, {
                "cnpj": cnpj,
                "status": "not_found",
                "message": "CNPJ não encontrado na base cadastral.",
            }
        query_info = build_google_geocoding_query(est)
        if not query_info:
            return cnpj, est, None, {
                "cnpj": cnpj,
                "status": "skipped_incomplete_address",
                "message": "Endereço incompleto (requer logradouro, município e UF).",
            }
        async with semaphore:
            res = await geocode_address_google(client, api_key, query_info)
        return cnpj, est, query_info, {"cnpj": cnpj, **res}

    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        outcomes = await asyncio.gather(*[_run_single(client, c) for c in unique_cnpjs])

    final_results: List[Dict[str, Any]] = []
    any_persisted = False

    for cnpj, est, query_info, result in outcomes:
        if result["status"] == "updated" and est is not None and query_info is not None:
            try:
                await persist_google_geocode_result(conn, est, query_info, result)
                any_persisted = True
            except Exception as exc:
                result = {
                    "cnpj": cnpj,
                    "status": "error",
                    "query_address": result.get("query_address"),
                    "message": f"Falha ao persistir na base geo: {exc}",
                }
        final_results.append(result)

    if any_persisted:
        await conn.commit()

    return final_results

"""Unit tests for FastAPI endpoints, request validation, and domain logic (no DB needed)."""

from unittest.mock import AsyncMock, patch
import pytest
from httpx import ASGITransport, AsyncClient

from brcompany_api.db import get_db_conn
from brcompany_api.main import app


@pytest.fixture
async def unit_client():
    """Create an async HTTP client with mocked DB dependency and disabled DB pool lifecycle."""
    async def mock_conn():
        mock = AsyncMock()
        yield mock

    app.dependency_overrides[get_db_conn] = mock_conn

    with patch("brcompany_api.main.init_db_pool", new_callable=AsyncMock), \
         patch("brcompany_api.main.close_db_pool", new_callable=AsyncMock):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_company_invalid_length(unit_client: AsyncClient):
    """Test that CNPJ with invalid length returns 400 Bad Request."""
    response = await unit_client.get("/api/v1/companies/12345")
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    assert "8 digits" in data["detail"] or "14 digits" in data["detail"]


@pytest.mark.asyncio
async def test_company_invalid_check_digits(unit_client: AsyncClient):
    """Test that full 14-digit CNPJ with invalid check digits returns 422 Unprocessable Content."""
    response = await unit_client.get("/api/v1/companies/00.000.000/0001-99")
    assert response.status_code == 422
    data = response.json()
    assert "detail" in data
    assert "check digits are invalid" in data["detail"]


@pytest.mark.asyncio
async def test_branches_invalid_cnpj_base(unit_client: AsyncClient):
    """Test that branches endpoint rejects invalid base CNPJ length with 400."""
    response = await unit_client.get("/api/v1/companies/1234/branches")
    assert response.status_code == 400
    data = response.json()
    assert "CNPJ base must contain 8 digits" in data["detail"]


@pytest.mark.asyncio
async def test_partners_invalid_cnpj_base(unit_client: AsyncClient):
    """Test that partners endpoint rejects invalid base CNPJ length with 400."""
    response = await unit_client.get("/api/v1/companies/999/partners")
    assert response.status_code == 400
    data = response.json()
    assert "CNPJ base must contain 8 digits" in data["detail"]


@pytest.mark.asyncio
async def test_health_healthy(unit_client: AsyncClient):
    """Test health check returns 200 when database probe succeeds."""
    mock_health = {"status": "healthy", "latency_ms": 1.25}
    with patch("brcompany_api.main.check_db_health", return_value=mock_health):
        response = await unit_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["database"]["status"] == "healthy"
        assert "X-Process-Time-Ms" in response.headers


@pytest.mark.asyncio
async def test_health_degraded(unit_client: AsyncClient):
    """Test health check returns 503 when database probe fails."""
    mock_health = {"status": "unhealthy", "error": "Connection refused"}
    with patch("brcompany_api.main.check_db_health", return_value=mock_health):
        response = await unit_client.get("/health")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["database"]["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_openapi_docs(unit_client: AsyncClient):
    """Test OpenAPI JSON and documentation availability."""
    res_docs = await unit_client.get("/docs")
    assert res_docs.status_code == 200

    res_openapi = await unit_client.get("/openapi.json")
    assert res_openapi.status_code == 200
    spec = res_openapi.json()
    assert spec["info"]["title"] == "py-brcompany-api"


@pytest.mark.asyncio
async def test_check_db_health_exception_handling(unit_client: AsyncClient):
    """Test that check_db_health catches database pool exceptions and returns 503 degraded."""
    with patch("brcompany_api.db.get_db_pool", side_effect=RuntimeError("Pool not initialized")):
        response = await unit_client.get("/health")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["database"]["status"] == "unhealthy"
        assert "Pool not initialized" in data["database"]["error"]


def test_security_passwords_jwt_and_keys():
    """Test scrypt password hashing, JWT creation/decoding, API keys, and disposable email check."""
    from brcompany_api.utils.security import (
        create_access_token,
        decode_access_token,
        generate_api_key,
        generate_otp_code,
        hash_api_key,
        hash_password,
        is_disposable_email,
        verify_password,
    )

    pwd_hash = hash_password("SenhaForte123!")
    assert verify_password("SenhaForte123!", pwd_hash) is True
    assert verify_password("SenhaErrada", pwd_hash) is False

    token = create_access_token(user_id="123e4567-e89b-12d3-a456-426614174000", email="user@empresa.com.br", role="admin")
    decoded = decode_access_token(token)
    assert decoded is not None
    assert decoded["sub"] == "123e4567-e89b-12d3-a456-426614174000"
    assert decoded["role"] == "admin"

    otp = generate_otp_code()
    assert len(otp) == 6 and otp.isdigit()

    full_key, prefix, key_hash = generate_api_key()
    assert full_key.startswith("ppj_live_")
    assert prefix == full_key[:16]
    assert hash_api_key(full_key) == key_hash

    assert is_disposable_email("teste@mailinator.com") is True
    assert is_disposable_email("diretoria@panoramapj.com.br") is False


def test_masking_and_lgpd_utilities():
    """Test phone, email, street, and CPF masking utilities."""
    from brcompany_api.utils.masking import (
        mask_cpf_or_tax_id,
        mask_email,
        mask_phone,
        mask_street,
    )

    assert mask_phone("11", "987654321") == "(11) ****-4321"
    assert mask_email("contato@panoramapj.com.br") == "co***@panoramapj.com.br"
    assert mask_street("AVENIDA", "PAULISTA", "1000") == "AVENIDA PAU***, Nº ***"
    assert mask_cpf_or_tax_id("12345678901") == "***456789**"
    assert mask_cpf_or_tax_id("00000000000191") == "00000000000191"


def test_spreadsheet_csv_and_xlsx_generation():
    """Verify CSV and native XLSX generation contain expected headers and valid ZIP/XML structure."""
    import io
    import zipfile
    from brcompany_api.utils.spreadsheet import generate_csv_bytes, generate_xlsx_bytes

    sample_rows = [
        {
            "cnpj": "00000000000191",
            "cnpj_base": "00000000",
            "identifier_type": "1",
            "legal_name": "PANORAMA TECNOLOGIA LTDA",
            "trade_name": "PANORAMAPJ",
            "start_activity_date": "2024-01-10",
            "main_cnae": "6201501",
            "main_cnae_description": "Desenvolvimento de programas de computador sob encomenda",
            "secondary_cnaes": ["6202300"],
            "legal_nature_code": "2062",
            "legal_nature_description": "Sociedade Empresária Limitada",
            "company_size": "01",
            "share_capital": 50000.00,
            "simples_optant": True,
            "mei_optant": False,
            "state": "SP",
            "municipality_name": "SAO PAULO",
            "neighborhood": "ITAIM BIBI",
            "street_type": "AVENIDA",
            "street": "BRIGADEIRO FARIA LIMA",
            "number": "3000",
            "complement": "CONJ 101",
            "postal_code": "04538132",
            "area_code_1": "11",
            "phone_1": "30001234",
            "email": "CONTATO@PANORAMAPJ.COM.BR",
            "partners_summary": "JOAO SILVA (Sócio-Administrador)",
        }
    ]

    csv_data = generate_csv_bytes(sample_rows).decode("utf-8-sig")
    assert "CNPJ;CNPJ Básico;Tipo (Matriz/Filial);Razão Social" in csv_data
    assert "00.000.000/0001-91;00000000;Matriz;PANORAMA TECNOLOGIA LTDA" in csv_data
    assert "contato@panoramapj.com.br" in csv_data

    xlsx_data = generate_xlsx_bytes(sample_rows)
    with zipfile.ZipFile(io.BytesIO(xlsx_data)) as zf:
        names = zf.namelist()
        assert "xl/workbook.xml" in names
        assert "xl/worksheets/sheet1.xml" in names
        sheet_xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "PANORAMA TECNOLOGIA LTDA" in sheet_xml
        assert "JOAO SILVA (Sócio-Administrador)" in sheet_xml


def test_search_filter_where_clause_builder():
    """Verify build_where_clause generates parameterized SQL for all B2B filters."""
    from brcompany_api.domains.search.schemas import SearchFilterRequest
    from brcompany_api.domains.search.service import build_where_clause

    filters = SearchFilterRequest(
        q="Tecnologia",
        states=["sp", "rj"],
        municipality_codes=["7107"],
        main_cnaes=["6201-5/01"],
        include_secondary_cnae=True,
        company_sizes=["01", "03"],
        simples_optant=True,
        mei_optant=False,
        only_with_phone=True,
        only_with_email=True,
    )
    where_sql, params = build_where_clause(filters, alias="csi", use_index_table=True)
    assert "csi.search_text ILIKE %(q_like)s" in where_sql
    assert "csi.state = ANY(%(states)s)" in where_sql
    assert "csi.municipality_code = ANY(%(municipality_codes)s)" in where_sql
    assert "csi.secondary_cnaes::text[] && %(main_cnaes)s::text[]" in where_sql
    assert "csi.has_phone = true" in where_sql
    assert "csi.has_email = true" in where_sql
    assert "company_opt_outs" in where_sql
    assert params["states"] == ["SP", "RJ"]
    assert params["main_cnaes"] == ["6201501"]


@pytest.mark.asyncio
async def test_checkout_plans_and_admin_protection(unit_client: AsyncClient):
    """Test public plans endpoint and verify admin settings endpoint rejects unauthenticated or non-admin users."""
    res_plans = await unit_client.get("/api/v1/checkout/plans")
    assert res_plans.status_code == 200
    plans = res_plans.json()
    codes = [p["code"] for p in plans]
    assert codes == ["starter", "pro", "scale", "smart_monthly"]

    # Unauthenticated request to /api/v1/admin/settings must return 401
    res_admin = await unit_client.get("/api/v1/admin/settings")
    assert res_admin.status_code == 401


def test_geo_place_name_normalization():
    """Verify accent-free uppercase normalization for RFB municipality matching."""
    from brcompany_api.utils.geo import BRAZIL_STATES, normalize_place_name

    assert "SP" in BRAZIL_STATES
    assert len(BRAZIL_STATES) == 27
    assert normalize_place_name("  São   Paulo ") == "SAO PAULO"
    assert normalize_place_name("Florianópolis") == "FLORIANOPOLIS"
    assert normalize_place_name("Brasília") == "BRASILIA"


@pytest.mark.asyncio
async def test_reverse_geocode_endpoint_and_service(unit_client: AsyncClient):
    """Verify /api/v1/domains/location/reverse validation and geo.addresses resolution."""
    from unittest.mock import MagicMock
    from brcompany_api.domains.metadata.service import reverse_geocode_coordinates

    # Out-of-range coordinates rejected with 422
    res_bad = await unit_client.get("/api/v1/domains/location/reverse?lat=99&lon=-46.6")
    assert res_bad.status_code == 422

    with patch(
        "brcompany_api.domains.metadata.service.reverse_geocode_coordinates",
        new_callable=AsyncMock,
        return_value={
            "detected": True,
            "state": "SP",
            "municipality_code": "7107",
            "municipality_name": "SAO PAULO",
            "source": "geo_addresses_city",
        },
    ) as mock_rev:
        res = await unit_client.get("/api/v1/domains/location/reverse?lat=-23.5505&lon=-46.6333")
        assert res.status_code == 200
        body = res.json()
        assert body["detected"] is True
        assert body["state"] == "SP"
        assert body["municipality_code"] == "7107"
        assert body["source"] == "geo_addresses_city"
        mock_rev.assert_awaited_once()

    # Test real reverse_geocode_coordinates matching logic directly
    mock_rows = [
        {"municipality_code": "6291", "municipality_name": "CAMPINAS"},
        {"municipality_code": "7107", "municipality_name": "SAO PAULO"},
    ]
    fake_cur = AsyncMock()
    fake_cur.fetchall.return_value = mock_rows
    fake_cur_ctx = AsyncMock()
    fake_cur_ctx.__aenter__.return_value = fake_cur
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cur_ctx

    with patch(
        "brcompany_api.domains.metadata.service._lookup_nearest_geo_address",
        new_callable=AsyncMock,
        return_value={"region": "SP", "city": "São Paulo"},
    ):
        resolved_city = await reverse_geocode_coordinates(fake_conn, -23.5505, -46.6333)
        assert resolved_city["source"] == "geo_addresses_city"
        assert resolved_city["municipality_code"] == "7107"

    with patch(
        "brcompany_api.domains.metadata.service._lookup_nearest_geo_address",
        new_callable=AsyncMock,
        return_value={"region": "SP", "city": "Cidade Inexistente"},
    ):
        resolved_state_only = await reverse_geocode_coordinates(fake_conn, -23.5505, -46.6333)
        assert resolved_state_only["source"] == "geo_addresses_state"
        assert resolved_state_only["state"] == "SP"
        assert resolved_state_only["municipality_code"] is None

    with patch(
        "brcompany_api.domains.metadata.service._lookup_nearest_geo_address",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resolved_none = await reverse_geocode_coordinates(fake_conn, 40.7128, -74.0060)
        assert resolved_none["detected"] is False
        assert resolved_none["source"] == "unavailable"


@pytest.mark.asyncio
async def test_search_filter_classification_and_cache(unit_client: AsyncClient):
    """Verify filter classification helpers and TTL+epoch search preview cache."""
    from brcompany_api.domains.search.schemas import MaskedCompanyPreviewItem, SearchFilterRequest
    from brcompany_api.domains.search.service import (
        get_cached_preview,
        invalidate_search_cache,
        is_location_only_search,
        is_unfiltered_search,
    )

    assert is_unfiltered_search(SearchFilterRequest()) is True
    assert is_unfiltered_search(SearchFilterRequest(states=[" "])) is True
    assert is_unfiltered_search(SearchFilterRequest(states=["SP"])) is False

    assert is_location_only_search(SearchFilterRequest(states=["SP"])) is True
    assert is_location_only_search(SearchFilterRequest(states=["SP"], municipality_codes=["7107"])) is True
    assert is_location_only_search(SearchFilterRequest(states=["SP"], main_cnaes=["6201501"])) is False
    assert is_location_only_search(SearchFilterRequest()) is False

    invalidate_search_cache()
    sample_item = MaskedCompanyPreviewItem(
        cnpj="00000000000191",
        cnpj_base="00000000",
        legal_name="EMPRESA TESTE LTDA",
    )
    fake_conn = AsyncMock()

    with patch("brcompany_api.domains.search.service.get_search_cache_epoch", new_callable=AsyncMock, return_value="24:0:"), \
         patch("brcompany_api.domains.search.service.count_matching_companies", new_callable=AsyncMock, return_value=100) as mock_count, \
         patch("brcompany_api.domains.search.service.preview_matching_companies", new_callable=AsyncMock, return_value=[sample_item]) as mock_prev:
        f = SearchFilterRequest(states=["SP"], municipality_codes=["7107"])
        total1, items1 = await get_cached_preview(fake_conn, f, limit=5, ttl_seconds=300, max_entries=32)
        total2, items2 = await get_cached_preview(fake_conn, f, limit=5, ttl_seconds=300, max_entries=32)
        assert total1 == 100 and total2 == 100
        assert len(items1) == 1 and len(items2) == 1
        assert mock_count.await_count == 1
        assert mock_prev.await_count == 1

        invalidate_search_cache()
        await get_cached_preview(fake_conn, f, limit=5, ttl_seconds=300, max_entries=32)
        assert mock_count.await_count == 2
        assert mock_prev.await_count == 2


def test_geo_normalization_and_street_candidates():
    """Verify CEP, UF, street number, and street candidate helpers."""
    from brcompany_api.domains.geo.service import (
        build_street_candidates,
        clean_postal_code,
        clean_state,
        clean_street_number,
    )

    assert clean_postal_code("01310-100") == "01310100"
    assert clean_postal_code("00000-000") is None
    assert clean_postal_code("123") is None

    assert clean_state(" sp ") == "SP"
    assert clean_state("XX") is None

    assert clean_street_number(" 1047 ") == "1047"
    assert clean_street_number("S/N") is None
    assert clean_street_number("***") is None

    candidates = build_street_candidates("AVENIDA", "Paulista")
    assert "avenida paulista" in candidates
    assert "av paulista" in candidates
    assert "paulista" in candidates


@pytest.mark.asyncio
async def test_search_map_endpoint_and_limit_validation(unit_client: AsyncClient):
    """Verify /api/v1/search/map caps limit at 100 and returns georeferenced items."""
    from brcompany_api.domains.search.schemas import (
        GeoRadiusFilter,
        MapCenterPoint,
        MapCompanyItem,
        SearchFilterRequest,
    )
    from brcompany_api.domains.search.service import is_location_only_search, is_unfiltered_search

    geo_filter = SearchFilterRequest(
        geo_radius=GeoRadiusFilter(latitude=-23.5614, longitude=-46.6559, radius_km=2.0)
    )
    assert is_unfiltered_search(geo_filter) is False
    assert is_location_only_search(geo_filter) is False

    res_over_limit = await unit_client.post(
        "/api/v1/search/map?limit=101",
        json={"states": ["SP"]},
    )
    assert res_over_limit.status_code == 422

    sample_map_item = MapCompanyItem(
        cnpj="00000000000191",
        cnpj_base="00000000",
        legal_name="EMPRESA MAPA LTDA",
        state="SP",
        municipality_name="SAO PAULO",
        latitude=-23.5614,
        longitude=-46.6559,
        geo_precision="cep_centroid",
    )
    with patch(
        "brcompany_api.domains.search.service.get_cached_map_preview",
        new_callable=AsyncMock,
        return_value=(
            42,
            [sample_map_item],
            MapCenterPoint(latitude=-23.5614, longitude=-46.6559),
        ),
    ) as mock_map:
        res = await unit_client.post(
            "/api/v1/search/map",
            json={
                "states": ["SP"],
                "geo_radius": {"latitude": -23.5614, "longitude": -46.6559, "radius_km": 2.0},
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total_companies"] == 42
        assert body["mapped_count"] == 1
        assert body["limit"] == 100
        assert body["center"]["latitude"] == pytest.approx(-23.5614)
        assert body["items"][0]["geo_precision"] == "cep_centroid"
        mock_map.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_establishment_geo_mei_masking():
    """Verify MEI LGPD masking prevents returning exact_number coordinates from cache."""
    from brcompany_api.domains.geo.service import resolve_establishment_geo

    fake_conn = AsyncMock()
    est = {
        "cnpj": "12345678000199",
        "state": "SP",
        "postal_code": "01310100",
        "number": "1000",
        "municipality_name": "SAO PAULO",
    }

    with patch(
        "brcompany_api.domains.geo.service._lookup_company_cache",
        new_callable=AsyncMock,
        return_value={
            "12345678000199": {
                "latitude": -23.5614,
                "longitude": -46.6559,
                "precision": "exact_number",
                "source": "br_company.geo",
            }
        },
    ), patch(
        "brcompany_api.domains.geo.service._lookup_postcode_centroids_batch",
        new_callable=AsyncMock,
        return_value={("SP", "01310100"): (-23.5620, -46.6550)},
    ), patch(
        "brcompany_api.domains.geo.service._save_company_cache_entries",
        new_callable=AsyncMock,
    ):
        masked = await resolve_establishment_geo(fake_conn, est, mask_exact=True)
        assert masked is not None
        assert masked["precision"] == "cep_centroid"

        unmasked = await resolve_establishment_geo(fake_conn, est, mask_exact=False)
        assert unmasked is not None
        assert unmasked["precision"] == "exact_number"




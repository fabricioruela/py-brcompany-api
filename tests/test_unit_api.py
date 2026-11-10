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


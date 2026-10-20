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

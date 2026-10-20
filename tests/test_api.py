"""Integration tests for FastAPI endpoints and database connectivity."""

from decimal import Decimal
import pytest
from httpx import ASGITransport, AsyncClient

import socket
from urllib.parse import urlparse
from brcompany_api.config import get_settings
from brcompany_api.db import get_db_pool
from brcompany_api.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def require_db():
    """Skip integration tests immediately if PostgreSQL is not reachable (e.g. in CI)."""
    cfg = get_settings()
    parsed = urlparse(cfg.app_database_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except OSError:
        pytest.skip(f"PostgreSQL database is not reachable at {host}:{port}; skipping integration tests.")


@pytest.fixture
async def client():
    """Create an async HTTP client connected to the FastAPI app with lifespan active."""
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["database"]["status"] == "healthy"
    assert "latency_ms" in data["database"]
    assert "X-Process-Time-Ms" in response.headers


@pytest.mark.asyncio
async def test_domains_cnaes(client: AsyncClient):
    response = await client.get("/api/v1/domains/cnaes?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] > 0
    assert len(data["items"]) <= 10
    assert "code" in data["items"][0]
    assert "description" in data["items"][0]


@pytest.mark.asyncio
async def test_domains_municipalities(client: AsyncClient):
    response = await client.get("/api/v1/domains/municipalities?limit=5")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] > 0
    assert len(data["items"]) <= 5


@pytest.mark.asyncio
async def test_domains_legal_natures(client: AsyncClient):
    response = await client.get("/api/v1/domains/legal-natures")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] > 0
    assert len(data["items"]) > 0


@pytest.mark.asyncio
async def test_system_status(client: AsyncClient):
    response = await client.get("/api/v1/status")
    assert response.status_code == 200
    data = response.json()
    assert data["current_schema"] == "registry"
    assert "last_published_month" in data


@pytest.mark.asyncio
async def test_company_invalid_cnpj(client: AsyncClient):
    # Invalid length
    res1 = await client.get("/api/v1/companies/12345")
    assert res1.status_code == 400

    # Invalid check digits
    res2 = await client.get("/api/v1/companies/00.000.000/0001-99")
    assert res2.status_code == 422


@pytest.mark.asyncio
async def test_company_full_lifecycle(client: AsyncClient):
    """Insert a test company and verify consolidated profile, branches, and QSA endpoints."""
    pool = get_db_pool()
    test_cnpj_base = "99887766"
    test_cnpj = "99887766000105"  # Valid test check digits (DV = 05)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Clean up before testing
            await cur.execute("DELETE FROM partner WHERE cnpj_base = %s;", (test_cnpj_base,))
            await cur.execute("DELETE FROM simples WHERE cnpj_base = %s;", (test_cnpj_base,))
            await cur.execute("DELETE FROM establishment WHERE cnpj_base = %s;", (test_cnpj_base,))
            await cur.execute("DELETE FROM company WHERE cnpj_base = %s;", (test_cnpj_base,))

            # 1. Insert company
            await cur.execute(
                """
                INSERT INTO company (cnpj_base, legal_name, legal_nature_code, responsible_qualification_code, share_capital, company_size)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (test_cnpj_base, "ACME TECH LTDA", "2062", "49", Decimal("150000.00"), "01"),
            )

            # 2. Insert establishment (matriz)
            await cur.execute(
                """
                INSERT INTO establishment (
                    cnpj_base, cnpj_order, cnpj_check_digits, identifier_type,
                    trade_name, registration_status, registration_status_date,
                    start_activity_date, main_cnae, secondary_cnaes,
                    street_type, street, number, neighborhood, postal_code, state, municipality_code,
                    email
                ) VALUES (
                    %s, '0001', '05', '1',
                    'ACME TECNOLOGIA', '02', '2020-01-15',
                    '2020-01-15', '6201501', ARRAY['6202300'],
                    'RUA', 'DAS FLORES', '100', 'CENTRO', '01001000', 'SP', '7107',
                    'contato@acme.com.br'
                );
                """,
                (test_cnpj_base,),
            )

            # 3. Insert simples
            await cur.execute(
                """
                INSERT INTO simples (cnpj_base, simples_optant, simples_opt_in_date, mei_optant)
                VALUES (%s, 'S', '2020-01-15', 'N');
                """,
                (test_cnpj_base,),
            )

            # 4. Insert partner
            await cur.execute(
                """
                INSERT INTO partner (
                    cnpj_base, partner_type, partner_name, partner_tax_id, qualification_code, age_group
                ) VALUES (
                    %s, '2', 'FULANO DE TAL', '***123456**', '49', '4'
                );
                """,
                (test_cnpj_base,),
            )
            await conn.commit()

    try:
        # Test 1: Fetch by full 14-digit masked CNPJ
        res_full = await client.get("/api/v1/companies/99.887.766/0001-05")
        assert res_full.status_code == 200
        data = res_full.json()

        assert data["company"]["cnpj_base"] == test_cnpj_base
        assert data["company"]["legal_name"] == "ACME TECH LTDA"
        assert data["company"]["company_size_description"] == "Microempresa (ME)"
        assert data["company"]["is_simples_optant"] is True

        assert data["establishment"]["cnpj"] == test_cnpj
        assert data["establishment"]["cnpj_formatted"] == "99.887.766/0001-05"
        assert data["establishment"]["identifier_type_description"] == "Matriz"
        assert data["establishment"]["registration_status_description"] == "Ativa"
        assert data["establishment"]["main_cnae"] == "6201501"

        assert len(data["partners"]) == 1
        assert data["partners"][0]["partner_name"] == "FULANO DE TAL"
        assert data["partners"][0]["partner_type_description"] == "Pessoa Física"
        assert data["partners"][0]["age_group_description"] == "31 a 40 anos"

        # Test 2: Fetch by 8-digit base CNPJ
        res_base = await client.get(f"/api/v1/companies/{test_cnpj_base}")
        assert res_base.status_code == 200
        assert res_base.json()["company"]["cnpj_base"] == test_cnpj_base

        # Test 3: List branches
        res_branches = await client.get(f"/api/v1/companies/{test_cnpj_base}/branches")
        assert res_branches.status_code == 200
        assert res_branches.json()["total"] == 1

        # Test 4: List partners
        res_partners = await client.get(f"/api/v1/companies/{test_cnpj_base}/partners")
        assert res_partners.status_code == 200
        assert res_partners.json()["total"] == 1
        assert res_partners.json()["partners"][0]["partner_name"] == "FULANO DE TAL"

    finally:
        # Clean up test rows
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM partner WHERE cnpj_base = %s;", (test_cnpj_base,))
                await cur.execute("DELETE FROM simples WHERE cnpj_base = %s;", (test_cnpj_base,))
                await cur.execute("DELETE FROM establishment WHERE cnpj_base = %s;", (test_cnpj_base,))
                await cur.execute("DELETE FROM company WHERE cnpj_base = %s;", (test_cnpj_base,))
                await conn.commit()

"""Unit tests for structured JSON logging, contextvars, request_id propagation, and telemetry."""

import json
import logging
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from brcompany_api.db import get_db_conn
from brcompany_api.logging_config import (
    configure_logging,
    get_current_context,
    log_context,
    reset_user_context,
    set_user_context,
)
from brcompany_api.main import app


def test_json_formatter_and_contextvars(capsys):
    configure_logging(level_name="INFO", log_format="json", env_name="production")
    logger = logging.getLogger("brcompany_api.test")

    with log_context(request_id="req-123", method="GET", path="/api/v1/companies/123", client_ip="10.0.0.1"):
        set_user_context("user-99", "jwt")
        logger.info("Fetching company", extra={"fields": {"event": "company_lookup", "status_code": 200}})
        reset_user_context()

    assert get_current_context() == {}
    record = json.loads(capsys.readouterr().out.strip())

    assert record["level"] == "INFO"
    assert record["service"] == "py-brcompany-api"
    assert record["env"] == "production"
    assert record["logger"] == "brcompany_api.test"
    assert record["message"] == "Fetching company"
    assert isinstance(record["pid"], int)
    assert record["timestamp"].endswith("Z")
    assert record["request_id"] == "req-123"
    assert record["method"] == "GET"
    assert record["path"] == "/api/v1/companies/123"
    assert record["client_ip"] == "10.0.0.1"
    assert record["user_id"] == "user-99"
    assert record["auth_method"] == "jwt"
    assert record["event"] == "company_lookup"
    assert record["status_code"] == 200


@pytest.mark.asyncio
async def test_request_id_propagation_and_client_error_telemetry(capsys):
    configure_logging(level_name="DEBUG", log_format="json", env_name="test")
    capsys.readouterr()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/telemetry/client-error",
            headers={"X-Request-ID": "nginx-req-abc"},
            json={
                "source": "api_fetch",
                "level": "warning",
                "message": "HTTP 404 em /api/v1/companies/123",
                "url": "https://brcompany.net.br/empresa/123",
                "endpoint": "/api/v1/companies/123",
                "http_method": "GET",
                "status_code": 404,
                "api_request_id": "req-prev-1",
            },
        )
    assert resp.status_code == 202
    assert resp.headers.get("X-Request-ID") == "nginx-req-abc"

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    telemetry_records = [r for r in lines if r.get("logger") == "brcompany_api.telemetry"]
    assert len(telemetry_records) == 1
    assert telemetry_records[0]["level"] == "WARNING"
    assert telemetry_records[0]["event"] == "client_error"
    assert telemetry_records[0]["status_code"] == 404
    assert telemetry_records[0]["request_id"] == "nginx-req-abc"

    http_records = [r for r in lines if r.get("logger") == "brcompany_api.http"]
    assert len(http_records) == 1
    assert http_records[0]["status_code"] == 202
    assert http_records[0]["request_id"] == "nginx-req-abc"


@pytest.mark.asyncio
async def test_generated_request_id_when_missing():
    async def mock_conn():
        yield AsyncMock()

    app.dependency_overrides[get_db_conn] = mock_conn
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/companies/12345")
        assert resp.status_code == 400
        generated = resp.headers.get("X-Request-ID")
        assert generated is not None and len(generated) == 32
    finally:
        app.dependency_overrides.clear()

"""Main FastAPI application module for PanoramaPJ (py-brcompany-api)."""

from typing import Optional
from contextlib import asynccontextmanager
import logging
import time
import uuid
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from brcompany_api.config import get_settings
from brcompany_api.db import check_db_health, close_db_pool, init_db_pool
from brcompany_api.domains.admin.router import router as admin_router
from brcompany_api.domains.auth.router import router as auth_router
from brcompany_api.domains.billing.router import router as billing_router
from brcompany_api.domains.company.router import router as company_router
from brcompany_api.domains.export.router import router as export_router
from brcompany_api.domains.metadata.router import router as metadata_router
from brcompany_api.domains.privacy.router import router as privacy_router
from brcompany_api.domains.radars.router import router as radars_router
from brcompany_api.domains.search.router import router as search_router
from brcompany_api.domains.telemetry.router import router as telemetry_router
from brcompany_api.logging_config import configure_logging, log_context, reset_user_context

settings = get_settings()

configure_logging(
    level_name=settings.app_log_level,
    log_format=settings.app_log_format,
    env_name=settings.app_env,
)
logger = logging.getLogger("brcompany_api")
http_logger = logging.getLogger("brcompany_api.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown lifecycle for connection pools."""
    configure_logging(
        level_name=settings.app_log_level,
        log_format=settings.app_log_format,
        env_name=settings.app_env,
    )
    logger.info("Starting up %s (%s)...", settings.app_name, settings.app_env)
    await init_db_pool(settings)
    yield
    logger.info("Shutting down %s...", settings.app_name)
    await close_db_pool()


app = FastAPI(
    title="py-brcompany-api",
    description=(
        "PanoramaPJ — Plataforma SaaS/DaaS de Inteligência de Mercado B2B, Consulta de CNPJ, "
        "Enriquecimento Cadastral, Exportação de Leads e Prospecção."
    ),
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Process-Time-Ms", "X-Export-List-Id", "X-Request-ID", "Content-Disposition"],
)


def _resolve_client_ip(request: Request) -> Optional[str]:
    x_real_ip = request.headers.get("X-Real-IP")
    if x_real_ip:
        return x_real_ip.strip()
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else None


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Bind request context, propagate X-Request-ID, and emit structured HTTP access logs."""
    request_id = (request.headers.get("X-Request-ID") or "").strip() or uuid.uuid4().hex
    client_ip = _resolve_client_ip(request)
    start_time = time.perf_counter()

    with log_context(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        user_id=None,
        auth_method=None,
        client_ip=client_ip,
    ):
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            http_logger.exception(
                "%s %s -> 500 (%.2fms)",
                request.method,
                request.url.path,
                duration_ms,
                extra={
                    "fields": {
                        "event": "http_request",
                        "status_code": 500,
                        "duration_ms": duration_ms,
                        "query": request.url.query or None,
                    }
                },
            )
            reset_user_context()
            raise

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"
        response.headers["X-Request-ID"] = request_id

        fields = {
            "event": "http_request",
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "query": request.url.query or None,
        }
        if request.url.path == "/health" and response.status_code < 400:
            http_logger.debug("%s %s -> %d (%.2fms)", request.method, request.url.path, response.status_code, duration_ms, extra={"fields": fields})
        elif response.status_code >= 500:
            http_logger.error("%s %s -> %d (%.2fms)", request.method, request.url.path, response.status_code, duration_ms, extra={"fields": fields})
        elif response.status_code >= 400:
            http_logger.warning("%s %s -> %d (%.2fms)", request.method, request.url.path, response.status_code, duration_ms, extra={"fields": fields})
        else:
            http_logger.info("%s %s -> %d (%.2fms)", request.method, request.url.path, response.status_code, duration_ms, extra={"fields": fields})

        reset_user_context()
        return response


@app.get(
    "/health",
    tags=["System"],
    summary="Health check probe",
    description="Checks API status and PostgreSQL database connectivity.",
)
async def health_check():
    db_status = await check_db_health()
    is_ok = db_status.get("status") == "healthy"
    return JSONResponse(
        status_code=200 if is_ok else 503,
        content={
            "status": "ok" if is_ok else "degraded",
            "version": settings.app_version,
            "database": db_status,
        },
    )


# Register API v1 routers
app.include_router(auth_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(search_router, prefix="/api/v1")
app.include_router(export_router, prefix="/api/v1")
app.include_router(billing_router, prefix="/api/v1")
app.include_router(radars_router, prefix="/api/v1")
app.include_router(privacy_router, prefix="/api/v1")
app.include_router(company_router, prefix="/api/v1")
app.include_router(metadata_router, prefix="/api/v1")
app.include_router(telemetry_router, prefix="/api/v1")


def start():
    """CLI script entrypoint."""
    uvicorn.run(
        "brcompany_api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=(settings.app_env == "development"),
        log_level=settings.app_log_level.lower(),
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    start()

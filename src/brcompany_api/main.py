"""Main FastAPI application module."""

from contextlib import asynccontextmanager
import logging
import time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

from brcompany_api.config import get_settings
from brcompany_api.db import check_db_health, close_db_pool, init_db_pool
from brcompany_api.domains.company.router import router as company_router
from brcompany_api.domains.metadata.router import router as metadata_router

settings = get_settings()

logging.basicConfig(
    level=settings.app_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("brcompany_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown lifecycle for connection pools."""
    logger.info("Starting up py-brcompany-api (%s)...", settings.app_env)
    await init_db_pool(settings)
    yield
    logger.info("Shutting down py-brcompany-api...")
    await close_db_pool()


app = FastAPI(
    title="py-brcompany-api",
    description="High-performance async REST API providing access to Brazilian Receita Federal CNPJ registry data.",
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
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Add response time in milliseconds to response headers."""
    start_time = time.perf_counter()
    response = await call_next(request)
    process_time = (time.perf_counter() - start_time) * 1000
    response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
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
app.include_router(company_router, prefix="/api/v1")
app.include_router(metadata_router, prefix="/api/v1")


def start():
    """CLI script entrypoint."""
    uvicorn.run(
        "brcompany_api.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=(settings.app_env == "development"),
        log_level=settings.app_log_level.lower(),
    )


if __name__ == "__main__":
    start()

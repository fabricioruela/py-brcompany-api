"""Application configuration and settings."""

from functools import lru_cache
from pathlib import Path
from typing import List, Union
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PanoramaPJ API (py-brcompany-api)"
    app_version: str = "0.2.0"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8040
    app_log_level: str = "INFO"

    # PostgreSQL configuration
    app_database_url: str = "postgresql://br_company:br_company_secret@localhost:5433/br_company"
    app_db_schema: str = "registry"
    app_saas_schema: str = "app_saas"
    app_db_pool_min_size: int = 4
    app_db_pool_max_size: int = 20
    app_db_pool_timeout: float = 30.0

    # Authentication & Security
    app_jwt_secret: str = "panoramapj-dev-secret-change-in-production-9f8e7d6c5b4a"
    app_jwt_expire_minutes: int = 60 * 24 * 7  # 7 days
    app_trial_credits: int = 25
    app_otp_expire_minutes: int = 15
    app_admin_emails: List[str] = [
        "fabricio.ruela@gmail.com",
        "admin@panoramapj.com.br",
        "admin@brcompany.net.br",
    ]

    # Export & Storage settings
    app_max_export_rows: int = 5000
    app_export_storage_dir: Path = Path("data/exports")
    app_export_retention_days: int = 30

    # Search Cache settings
    app_search_cache_ttl_seconds: int = 300
    app_search_cache_max_entries: int = 128

    # Google Geocoding API (server-side forced georeferencing)
    app_google_geocoding_api_key: str = ""
    app_google_geocoding_timeout_seconds: float = 10.0

    # Asaas Payment Gateway settings
    app_asaas_api_url: str = "https://sandbox.asaas.com/api/v3"
    app_asaas_api_key: str = ""
    app_asaas_webhook_token: str = "panoramapj-asaas-webhook-token"
    app_asaas_auto_nfse: bool = False
    app_asaas_municipal_service_code: str = ""

    # Cloudflare Edge Cache Purge (LGPD Opt-Out)
    app_cloudflare_zone_id: str = ""
    app_cloudflare_api_token: str = ""
    app_public_site_url: str = "https://brcompany.net.br"

    # CORS settings
    app_cors_origins: List[str] = [
        "http://brcompany.net.br",
        "https://brcompany.net.br",
        "http://www.brcompany.net.br",
        "https://www.brcompany.net.br",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

    @field_validator("app_cors_origins", "app_admin_emails", mode="before")
    @classmethod
    def assemble_string_list(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            if v.startswith("[") and v.endswith("]"):
                import json
                try:
                    return json.loads(v)
                except Exception:
                    pass
            return [i.strip() for i in v.split(",") if i.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Pydantic schemas for B2B Export Calculation, Download, and 30-Day List History."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, computed_field

from brcompany_api.domains.search.schemas import SearchFilterRequest


class ExportCalculateRequest(BaseModel):
    filters: SearchFilterRequest
    desired_quantity: int = Field(
        default=100,
        ge=1,
        le=5000,
        description="Quantidade desejada de empresas para exportar neste arquivo (máx. 5.000)",
    )
    skip_already_exported: bool = Field(
        default=True,
        description="Quando a desduplicação da plataforma está ativa: se True, busca apenas empresas inéditas; se False, inclui já exportadas sem cobrar créditos repetidos.",
    )


class ExportCalculateResponse(BaseModel):
    total_matching: int
    already_exported_in_search: int
    desired_quantity: int
    exportable_quantity: int
    already_exported_included_free: int
    net_credits_required: int
    user_balance: int
    has_sufficient_balance: bool
    deduplication_enabled: bool
    max_export_rows: int = 5000


class ExportDownloadRequest(BaseModel):
    list_name: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Nome opcional para identificar a lista no histórico",
    )
    filters: SearchFilterRequest
    desired_quantity: int = Field(default=100, ge=1, le=5000)
    skip_already_exported: bool = Field(default=True)
    format: Literal["csv", "xlsx"] = Field(default="csv")


class ExportListHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    filters_json: Dict[str, Any] = Field(default_factory=dict)
    total_records: int
    credits_charged: int
    deduplicated: bool
    expires_at: datetime
    created_at: datetime

    @computed_field
    @property
    def is_expired(self) -> bool:
        now = datetime.now(self.expires_at.tzinfo)
        return self.expires_at <= now

    @computed_field
    @property
    def days_remaining(self) -> int:
        now = datetime.now(self.expires_at.tzinfo)
        diff = (self.expires_at - now).days
        return max(0, diff)


class ExportHistoryResponse(BaseModel):
    total: int
    items: List[ExportListHistoryItem]

"""Schemas for domain tables and synchronization status."""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict


class DomainItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    description: str


class DomainListResponse(BaseModel):
    total: int
    items: List[DomainItem]


class EtlStatusResponse(BaseModel):
    current_schema: str
    last_published_month: Optional[str] = None
    publish_mode: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    total_companies: Optional[int] = None
    total_establishments: Optional[int] = None

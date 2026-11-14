"""Pydantic schemas for B2B Advanced Search, Count, and Masked Preview."""

from datetime import date
from decimal import Decimal
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, computed_field

from brcompany_api.domains.company.schemas import COMPANY_SIZE_LABELS, IDENTIFIER_TYPE_LABELS
from brcompany_api.utils.cnpj import format_cnpj


class GeoRadiusFilter(BaseModel):
    """Circular spatial filter centered at (latitude, longitude) with radius in km."""

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    radius_km: float = Field(default=3.0, ge=0.2, le=50.0)


class GeoBoundsFilter(BaseModel):
    """Rectangular map viewport bounds filter."""

    north: float = Field(..., ge=-90.0, le=90.0)
    south: float = Field(..., ge=-90.0, le=90.0)
    east: float = Field(..., ge=-180.0, le=180.0)
    west: float = Field(..., ge=-180.0, le=180.0)


class SearchFilterRequest(BaseModel):
    """Granular B2B prospecting filters for PanoramaPJ."""

    q: Optional[str] = Field(
        default=None,
        description="Termo em Razão Social ou Nome Fantasia",
    )
    states: List[str] = Field(
        default_factory=list,
        description="Lista de UFs (ex.: ['SP', 'MG'])",
    )
    municipality_codes: List[str] = Field(
        default_factory=list,
        description="Lista de códigos de municípios da Receita Federal (4 dígitos)",
    )
    neighborhoods: List[str] = Field(
        default_factory=list,
        description="Lista de bairros",
    )
    main_cnaes: List[str] = Field(
        default_factory=list,
        description="Lista de códigos CNAE de 7 dígitos",
    )
    secondary_cnaes: List[str] = Field(
        default_factory=list,
        description="Lista de códigos CNAE secundários",
    )
    include_secondary_cnae: bool = Field(
        default=False,
        description="Se verdadeiro, busca os main_cnaes também no array de CNAEs secundários",
    )
    company_sizes: List[str] = Field(
        default_factory=list,
        description="Portes: '01' (ME), '03' (EPP), '05' (Demais), '00' (Não informado)",
    )
    legal_nature_codes: List[str] = Field(
        default_factory=list,
        description="Códigos de natureza jurídica (4 dígitos)",
    )
    identifier_type: Optional[str] = Field(
        default=None,
        description="'1' para Matriz, '2' para Filial, ou None para ambos",
    )
    simples_optant: Optional[bool] = Field(
        default=None,
        description="Filtrar por optantes do Simples Nacional",
    )
    mei_optant: Optional[bool] = Field(
        default=None,
        description="Filtrar por Microempreendedor Individual (MEI)",
    )
    start_date_from: Optional[date] = Field(
        default=None,
        description="Data de abertura inicial (YYYY-MM-DD)",
    )
    start_date_to: Optional[date] = Field(
        default=None,
        description="Data de abertura final (YYYY-MM-DD)",
    )
    min_share_capital: Optional[Decimal] = Field(
        default=None,
        description="Capital social mínimo (R$)",
    )
    max_share_capital: Optional[Decimal] = Field(
        default=None,
        description="Capital social máximo (R$)",
    )
    only_with_phone: bool = Field(
        default=False,
        description="Apenas empresas com telefone cadastrado",
    )
    only_with_email: bool = Field(
        default=False,
        description="Apenas empresas com e-mail cadastrado",
    )
    geo_radius: Optional[GeoRadiusFilter] = Field(
        default=None,
        description="Filtro espacial circular (centro + raio em km)",
    )
    geo_bounds: Optional[GeoBoundsFilter] = Field(
        default=None,
        description="Filtro espacial por área retangular visível no mapa",
    )


class SearchCountResponse(BaseModel):
    total_companies: int
    execution_time_ms: float


class MaskedCompanyPreviewItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cnpj: str
    cnpj_base: str
    identifier_type: Optional[str] = None
    legal_name: str
    trade_name: Optional[str] = None
    start_activity_date: Optional[date] = None
    main_cnae: Optional[str] = None
    main_cnae_description: Optional[str] = None
    company_size: Optional[str] = None
    share_capital: Optional[Decimal] = None
    simples_optant: bool = False
    mei_optant: bool = False
    state: Optional[str] = None
    municipality_code: Optional[str] = None
    municipality_name: Optional[str] = None
    neighborhood: Optional[str] = None
    street_masked: Optional[str] = None
    phone_masked: Optional[str] = None
    email_masked: Optional[str] = None
    has_phone: bool = False
    has_email: bool = False
    geo_precision: Optional[str] = None
    geo_source: Optional[str] = None

    @computed_field
    @property
    def cnpj_formatted(self) -> str:
        return format_cnpj(self.cnpj)

    @computed_field
    @property
    def identifier_type_description(self) -> Optional[str]:
        return IDENTIFIER_TYPE_LABELS.get(self.identifier_type) if self.identifier_type else None

    @computed_field
    @property
    def company_size_description(self) -> Optional[str]:
        return COMPANY_SIZE_LABELS.get(self.company_size) if self.company_size else None


class SearchPreviewResponse(BaseModel):
    total_companies: int
    preview_count: int
    page: int = 1
    page_size: int = 50
    total_pages: int = 0
    items: List[MaskedCompanyPreviewItem]
    execution_time_ms: float


class MapCompanyItem(MaskedCompanyPreviewItem):
    latitude: float
    longitude: float
    geo_precision: str


class MapClusterItem(BaseModel):
    cluster_key: str
    label: str
    latitude: float
    longitude: float
    count: int
    postal_code_prefix: Optional[str] = None
    neighborhood: Optional[str] = None
    municipality_name: Optional[str] = None
    state: Optional[str] = None


class MapCenterPoint(BaseModel):
    latitude: float
    longitude: float


class MapSearchResponse(BaseModel):
    total_companies: int
    mapped_count: int
    limit: int
    offset: int = 0
    has_more: bool = False
    next_offset: Optional[int] = None
    mode: Literal["pins", "clusters"] = "pins"
    center: Optional[MapCenterPoint] = None
    items: List[MapCompanyItem] = Field(default_factory=list)
    clusters: List[MapClusterItem] = Field(default_factory=list)
    execution_time_ms: float

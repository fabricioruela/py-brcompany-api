"""Pydantic schemas for Company, Establishment, and Partner domain."""

from datetime import date
from decimal import Decimal
from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field, computed_field

from brcompany_api.utils.cnpj import format_cnpj

COMPANY_SIZE_LABELS = {
    "00": "Não Informado",
    "01": "Microempresa (ME)",
    "03": "Empresa de Pequeno Porte (EPP)",
    "05": "Demais",
}

IDENTIFIER_TYPE_LABELS = {
    "1": "Matriz",
    "2": "Filial",
}

REGISTRATION_STATUS_LABELS = {
    "01": "Nula",
    "02": "Ativa",
    "03": "Suspensa",
    "04": "Inapta",
    "08": "Baixada",
}

PARTNER_TYPE_LABELS = {
    "1": "Pessoa Jurídica",
    "2": "Pessoa Física",
    "3": "Estrangeiro",
}

AGE_GROUP_LABELS = {
    "0": "Não se aplica",
    "1": "0 a 12 anos",
    "2": "13 a 20 anos",
    "3": "21 a 30 anos",
    "4": "31 a 40 anos",
    "5": "41 a 50 anos",
    "6": "51 a 60 anos",
    "7": "61 a 70 anos",
    "8": "71 a 80 anos",
    "9": "Mais de 80 anos",
}


class CnaeItem(BaseModel):
    code: str
    description: Optional[str] = None


class PartnerItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    cnpj_base: str
    partner_type: Optional[str] = None
    partner_name: Optional[str] = None
    partner_tax_id: Optional[str] = None
    qualification_code: Optional[str] = None
    qualification_description: Optional[str] = None
    entry_date: Optional[date] = None
    country_code: Optional[str] = None
    country_name: Optional[str] = None
    legal_representative_tax_id: Optional[str] = None
    legal_representative_name: Optional[str] = None
    legal_representative_qualification_code: Optional[str] = None
    age_group: Optional[str] = None

    @computed_field
    @property
    def partner_type_description(self) -> Optional[str]:
        return PARTNER_TYPE_LABELS.get(self.partner_type) if self.partner_type else None

    @computed_field
    @property
    def age_group_description(self) -> Optional[str]:
        return AGE_GROUP_LABELS.get(self.age_group) if self.age_group else None


class EstablishmentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cnpj: str
    cnpj_base: str
    cnpj_order: str
    cnpj_check_digits: str
    identifier_type: Optional[str] = None
    trade_name: Optional[str] = None
    registration_status: Optional[str] = None
    registration_status_reason_description: Optional[str] = None
    registration_status_date: Optional[date] = None
    start_activity_date: Optional[date] = None
    main_cnae: Optional[str] = None
    main_cnae_description: Optional[str] = None
    secondary_cnaes: Optional[List[str]] = None
    street_type: Optional[str] = None
    street: Optional[str] = None
    number: Optional[str] = None
    complement: Optional[str] = None
    neighborhood: Optional[str] = None
    postal_code: Optional[str] = None
    state: Optional[str] = None
    municipality_code: Optional[str] = None
    municipality_name: Optional[str] = None
    area_code_1: Optional[str] = None
    phone_1: Optional[str] = None
    email: Optional[str] = None

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
    def registration_status_description(self) -> Optional[str]:
        return REGISTRATION_STATUS_LABELS.get(self.registration_status) if self.registration_status else None

    @computed_field
    @property
    def postal_code_formatted(self) -> Optional[str]:
        if self.postal_code and len(self.postal_code.strip()) == 8:
            clean = self.postal_code.strip()
            return f"{clean[:5]}-{clean[5:]}"
        return self.postal_code


class CompanyBaseInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cnpj_base: str
    legal_name: Optional[str] = None
    legal_nature_code: Optional[str] = None
    legal_nature_description: Optional[str] = None
    responsible_qualification_code: Optional[str] = None
    responsible_qualification_description: Optional[str] = None
    share_capital: Optional[Decimal] = None
    company_size: Optional[str] = None
    responsible_federative_entity: Optional[str] = None

    # Simples / MEI
    simples_optant: Optional[str] = None
    simples_opt_in_date: Optional[date] = None
    simples_opt_out_date: Optional[date] = None
    mei_optant: Optional[str] = None
    mei_opt_in_date: Optional[date] = None
    mei_opt_out_date: Optional[date] = None

    @computed_field
    @property
    def cnpj_base_formatted(self) -> str:
        return format_cnpj(self.cnpj_base)

    @computed_field
    @property
    def company_size_description(self) -> Optional[str]:
        return COMPANY_SIZE_LABELS.get(self.company_size) if self.company_size else None

    @computed_field
    @property
    def is_simples_optant(self) -> bool:
        return self.simples_optant == "S"

    @computed_field
    @property
    def is_mei_optant(self) -> bool:
        return self.mei_optant == "S"


class CompanyProfileResponse(BaseModel):
    """Consolidated response returning full corporate profile, establishment, and QSA."""
    company: CompanyBaseInfo
    establishment: EstablishmentSummary
    secondary_cnaes_details: List[CnaeItem] = Field(default_factory=list)
    partners: List[PartnerItem] = Field(default_factory=list)
    total_branches: int = 1


class BranchListResponse(BaseModel):
    """List of branches (filiais) for a base CNPJ."""
    cnpj_base: str
    total: int
    branches: List[EstablishmentSummary]


class PartnerListResponse(BaseModel):
    """List of partners (QSA) for a base CNPJ."""
    cnpj_base: str
    total: int
    partners: List[PartnerItem]

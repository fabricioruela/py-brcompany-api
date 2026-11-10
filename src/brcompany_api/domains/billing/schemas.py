"""Pydantic schemas and plans catalog for Checkout, Asaas Webhooks, and FIFO Wallet."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


PLANS_CATALOG: Dict[str, Dict[str, Any]] = {
    "starter": {
        "code": "starter",
        "name": "Pacote Starter",
        "kind": "avulso",
        "amount_cents": 3990,
        "price_formatted": "R$ 39,90",
        "credits": 400,
        "cost_per_lead": "R$ 0,10",
        "expires_in_days": None,
        "description": "Focado em microempresas e freelancers. Créditos sem data de expiração.",
        "features": [
            "400 créditos de exportação/API",
            "Créditos não expiram",
            "Exportação CSV e Excel (.xlsx)",
            "Desduplicação automática de CNPJs",
        ],
    },
    "pro": {
        "code": "pro",
        "name": "Pacote Pro (Carro-Chefe)",
        "kind": "avulso",
        "amount_cents": 9700,
        "price_formatted": "R$ 97,00",
        "credits": 1500,
        "cost_per_lead": "R$ 0,06",
        "expires_in_days": None,
        "badge": "Mais Vendido",
        "description": "Ideal para PMEs e SDRs em prospecção ativa.",
        "features": [
            "1.500 créditos de exportação/API",
            "Créditos não expiram",
            "Exportação CSV e Excel (.xlsx) com QSA",
            "Desduplicação automática + Chave de API",
        ],
    },
    "scale": {
        "code": "scale",
        "name": "Pacote Scale",
        "kind": "avulso",
        "amount_cents": 24700,
        "price_formatted": "R$ 247,00",
        "credits": 5000,
        "cost_per_lead": "R$ 0,05",
        "expires_in_days": None,
        "description": "Para operações comerciais intensivas e integrações via API REST.",
        "features": [
            "5.000 créditos de exportação/API",
            "Créditos não expiram",
            "Acesso completo à API DaaS (X-API-Key)",
            "Até 5.000 leads por planilha exportada",
        ],
    },
    "smart_monthly": {
        "code": "smart_monthly",
        "name": "Assinatura Smart (Mensal)",
        "kind": "subscription",
        "amount_cents": 7990,
        "price_formatted": "R$ 79,90/mês",
        "credits": 1500,
        "cost_per_lead": "R$ 0,05",
        "expires_in_days": 90,
        "badge": "Melhor Custo-Benefício",
        "description": "1.500 créditos/mês cumulativos por até 90 dias + Radares semanais de novas empresas.",
        "features": [
            "1.500 créditos todo mês (cumulativos por 90 dias)",
            "Consumo inteligente FIFO (usa primeiro os que vencem antes)",
            "Radares Salvos: alertas semanais de novas empresas no nicho",
            "Sem fidelidade (cancele quando quiser)",
        ],
    },
}


class PlanItem(BaseModel):
    code: str
    name: str
    kind: Literal["avulso", "subscription"]
    amount_cents: int
    price_formatted: str
    credits: int
    cost_per_lead: str
    expires_in_days: Optional[int] = None
    badge: Optional[str] = None
    description: str
    features: List[str]


class CheckoutPixRequest(BaseModel):
    plan_code: Literal["starter", "pro", "scale", "smart_monthly"]
    tax_id: Optional[str] = Field(
        default=None,
        description="CPF ou CNPJ do comprador (exigido pelo Asaas para PIX e emissão de NFS-e)",
    )


class CheckoutSubscriptionRequest(BaseModel):
    payment_method: Literal["pix", "credit_card"] = "pix"
    tax_id: Optional[str] = None


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plan_code: str
    payment_method: str
    status: str
    amount_cents: int
    credits_amount: int
    pix_qr_code: Optional[str] = None
    pix_copy_paste: Optional[str] = None
    nfse_url: Optional[str] = None
    nfse_status: Optional[str] = None
    expires_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    created_at: datetime


class CreditLotItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_type: str
    initial_amount: int
    remaining_amount: int
    expires_at: Optional[datetime] = None
    created_at: datetime


class CreditTransactionItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    transaction_type: str
    amount: int
    balance_after: int
    description: Optional[str] = None
    created_at: datetime


class WalletOverviewResponse(BaseModel):
    credit_balance: int
    has_active_subscription: bool
    active_lots: List[CreditLotItem]
    transactions: List[CreditTransactionItem]
    orders: List[OrderResponse]

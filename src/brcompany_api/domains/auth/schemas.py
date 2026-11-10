"""Pydantic schemas for Authentication, User Profile, and API Keys."""

from datetime import datetime
from typing import List, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=255)
    email: str = Field(..., min_length=5, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=30)
    password: str = Field(..., min_length=6, max_length=128)


class RegisterResponse(BaseModel):
    user_id: UUID
    email: str
    full_name: str
    role: str
    is_email_verified: bool
    verification_required: bool = True
    message: str
    dev_otp_code: Optional[str] = None


class VerifyEmailRequest(BaseModel):
    email: str
    otp_code: str = Field(..., min_length=6, max_length=6)


class ResendOtpRequest(BaseModel):
    email: str


class LoginRequest(BaseModel):
    email: str
    password: str


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=30)
    tax_id: Optional[str] = Field(default=None, max_length=20)


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    full_name: str
    phone: Optional[str] = None
    tax_id: Optional[str] = None
    role: str = "user"
    is_email_verified: bool = False
    credit_balance: int = 0
    has_active_subscription: bool = False
    created_at: Optional[datetime] = None


class AuthTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserProfile


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)


class ApiKeyItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    key_prefix: str
    is_active: bool
    last_used_at: Optional[datetime] = None
    created_at: datetime


class ApiKeyCreatedResponse(ApiKeyItem):
    api_key: str


class ApiKeyListResponse(BaseModel):
    total: int
    items: List[ApiKeyItem]

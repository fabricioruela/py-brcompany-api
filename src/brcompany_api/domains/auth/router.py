"""FastAPI router for Authentication, Email OTP Verification, Profile, and API Keys."""

from typing import Annotated
from uuid import UUID
import psycopg
from fastapi import APIRouter, Depends, Request, status

from brcompany_api.config import Settings, get_settings
from brcompany_api.db import get_db_conn
from brcompany_api.domains.auth import service
from brcompany_api.domains.auth.dependencies import get_current_user
from brcompany_api.domains.auth.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyItem,
    ApiKeyListResponse,
    AuthTokenResponse,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    ResendOtpRequest,
    UpdateProfileRequest,
    UserProfile,
    VerifyEmailRequest,
)
from brcompany_api.utils.security import create_access_token

router = APIRouter(prefix="/auth", tags=["Authentication & API Keys"])


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register User & Send 6-Digit Email OTP",
)
async def register(
    body: RegisterRequest,
    request: Request,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    client_ip = request.client.host if request.client else None
    user_row, otp_code = await service.register_user(
        conn,
        full_name=body.full_name,
        email=body.email,
        password=body.password,
        phone=body.phone,
        ip_address=client_ip,
    )
    return RegisterResponse(
        user_id=user_row["id"],
        email=user_row["email"],
        full_name=user_row["full_name"],
        role=user_row["role"],
        is_email_verified=user_row["is_email_verified"],
        verification_required=True,
        message=(
            f"Cadastro criado! Digite o código de 6 dígitos enviado para {user_row['email']} "
            f"para ativar seus {settings.app_trial_credits} créditos cortesia."
        ),
        dev_otp_code=otp_code if settings.app_env != "production" else None,
    )


@router.post(
    "/verify-email",
    response_model=AuthTokenResponse,
    summary="Verify 6-Digit Email OTP & Grant 25 Trial Credits",
)
async def verify_email(
    body: VerifyEmailRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    profile = await service.verify_email_otp(conn, email=body.email, otp_code=body.otp_code)
    token = create_access_token(
        user_id=str(profile["id"]),
        email=profile["email"],
        role=profile["role"],
    )
    return AuthTokenResponse(
        access_token=token,
        user=UserProfile.model_validate(profile),
    )


@router.post(
    "/resend-otp",
    summary="Resend 6-Digit Email Verification OTP",
)
async def resend_otp(
    body: ResendOtpRequest,
    request: Request,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    client_ip = request.client.host if request.client else None
    otp_code = await service.resend_otp_code(conn, email=body.email, ip_address=client_ip)
    return {
        "message": "Novo código de verificação enviado para o seu e-mail.",
        "dev_otp_code": otp_code if settings.app_env != "production" else None,
    }


@router.post(
    "/login",
    response_model=AuthTokenResponse,
    summary="Authenticate User with Email & Password",
)
async def login(
    body: LoginRequest,
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    profile = await service.authenticate_user(conn, email=body.email, password=body.password)
    token = create_access_token(
        user_id=str(profile["id"]),
        email=profile["email"],
        role=profile["role"],
    )
    return AuthTokenResponse(
        access_token=token,
        user=UserProfile.model_validate(profile),
    )


@router.get(
    "/me",
    response_model=UserProfile,
    summary="Get Current Authenticated User & Wallet Summary",
)
async def get_me(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    return UserProfile.model_validate(current_user)


@router.patch(
    "/profile",
    response_model=UserProfile,
    summary="Update User Profile (Name, Phone, CPF/CNPJ)",
)
async def update_profile(
    body: UpdateProfileRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    updated = await service.update_user_profile(
        conn,
        user_id=current_user["id"],
        full_name=body.full_name,
        phone=body.phone,
        tax_id=body.tax_id,
    )
    return UserProfile.model_validate(updated)


@router.get(
    "/api-keys",
    response_model=ApiKeyListResponse,
    summary="List User API Keys",
)
async def list_api_keys(
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    rows = await service.list_user_api_keys(conn, current_user["id"])
    return ApiKeyListResponse(
        total=len(rows),
        items=[ApiKeyItem.model_validate(r) for r in rows],
    )


@router.post(
    "/api-keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate New API Key for DaaS Requests",
)
async def create_api_key(
    body: ApiKeyCreateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    row = await service.create_user_api_key(conn, current_user["id"], body.name)
    return ApiKeyCreatedResponse.model_validate(row)


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke API Key",
)
async def delete_api_key(
    key_id: UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
):
    await service.revoke_user_api_key(conn, current_user["id"], key_id)

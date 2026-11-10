"""FastAPI dependencies for JWT Bearer authentication, X-API-Key, and Admin role enforcement."""

from typing import Annotated, Optional
from uuid import UUID
import psycopg
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from brcompany_api.db import get_db_conn
from brcompany_api.domains.auth import service
from brcompany_api.utils.security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


async def get_optional_user(
    conn: Annotated[psycopg.AsyncConnection, Depends(get_db_conn)],
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)] = None,
    x_api_key: Annotated[Optional[str], Header(alias="X-API-Key")] = None,
) -> Optional[dict]:
    """Resolve authenticated user via Bearer JWT or X-API-Key, or return None if anonymous."""
    if credentials and credentials.credentials:
        payload = decode_access_token(credentials.credentials)
        if payload and payload.get("sub"):
            try:
                user_id = UUID(str(payload["sub"]))
                user = await service.get_user_profile_by_id(conn, user_id)
                if user:
                    user["auth_method"] = "jwt"
                    return user
            except Exception:
                pass

    if x_api_key and x_api_key.strip():
        user = await service.get_user_by_api_key(conn, x_api_key.strip())
        if user:
            user["auth_method"] = "api_key"
            return user

    return None


async def get_current_user(
    user: Annotated[Optional[dict], Depends(get_optional_user)],
) -> dict:
    """Require an authenticated user (via JWT or X-API-Key)."""
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticação necessária. Faça login ou informe uma chave X-API-Key válida.",
        )
    return user


async def require_admin_user(
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Require an authenticated platform Administrator (`role == 'admin'`)."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso restrito ao administrador da plataforma.",
        )
    return user

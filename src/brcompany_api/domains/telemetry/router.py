"""Client-side telemetry endpoint for capturing browser errors in journald."""

import logging
from typing import Annotated, Any, Literal, Optional
from fastapi import APIRouter, Header, Request, status
from pydantic import BaseModel, Field

from brcompany_api.logging_config import set_user_context
from brcompany_api.utils.security import decode_access_token

logger = logging.getLogger("brcompany_api.telemetry")

router = APIRouter(prefix="/telemetry", tags=["System"])


class ClientErrorReport(BaseModel):
    source: str = Field(..., min_length=1, max_length=64)
    level: Literal["warning", "error"] = "error"
    message: str = Field(..., min_length=1, max_length=2000)
    stack: Optional[str] = Field(default=None, max_length=8000)
    component_stack: Optional[str] = Field(default=None, max_length=4000)
    url: Optional[str] = Field(default=None, max_length=2000)
    endpoint: Optional[str] = Field(default=None, max_length=500)
    http_method: Optional[str] = Field(default=None, max_length=16)
    status_code: Optional[int] = Field(default=None, ge=100, le=599)
    api_request_id: Optional[str] = Field(default=None, max_length=128)
    user_agent: Optional[str] = Field(default=None, max_length=512)
    metadata: Optional[dict[str, Any]] = None


def _try_bind_jwt_user(authorization: Optional[str]) -> None:
    """Best-effort JWT user extraction without hitting PostgreSQL."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return
    token = authorization[7:].strip()
    if not token:
        return
    try:
        payload = decode_access_token(token)
        if payload and payload.get("sub"):
            set_user_context(str(payload["sub"]), "jwt")
    except Exception:
        pass


@router.post(
    "/client-error",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest browser client-side error telemetry",
)
async def report_client_error(
    body: ClientErrorReport,
    request: Request,
    authorization: Annotated[Optional[str], Header()] = None,
):
    _try_bind_jwt_user(authorization)

    fields = {
        "event": "client_error",
        "client_source": body.source,
        "client_url": body.url,
        "endpoint": body.endpoint,
        "http_method": body.http_method,
        "status_code": body.status_code,
        "api_request_id": body.api_request_id,
        "user_agent": body.user_agent or request.headers.get("User-Agent"),
        "stack": body.stack,
        "component_stack": body.component_stack,
        "client_metadata": body.metadata,
    }
    log_fn = logger.warning if body.level == "warning" else logger.error
    log_fn("Client error [%s]: %s", body.source, body.message, extra={"fields": fields})
    return {"status": "accepted"}

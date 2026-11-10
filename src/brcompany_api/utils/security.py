"""Security utilities: password hashing, JWT tokens, OTP codes, API keys, and disposable email checks."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Dict, Optional, Tuple

from brcompany_api.config import get_settings

DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com",
    "tempmail.com",
    "10minutemail.com",
    "guerrillamail.com",
    "yopmail.com",
    "sharklasers.com",
    "trashmail.com",
    "getnada.com",
    "temp-mail.org",
    "maildrop.cc",
    "dispostable.com",
    "fakeinbox.com",
    "throwawaymail.com",
    "mohmal.com",
}


def is_disposable_email(email: str) -> bool:
    """Return True if the email belongs to a known disposable/temporary email provider."""
    if not email or "@" not in email:
        return True
    domain = email.strip().lower().rsplit("@", 1)[-1]
    return domain in DISPOSABLE_EMAIL_DOMAINS


def hash_password(password: str) -> str:
    """Hash a plaintext password using scrypt with a random 16-byte salt."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored scrypt hash."""
    try:
        scheme, salt_hex, hash_hex = stored_hash.split("$", 2)
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        derived = hashlib.scrypt(
            plain_password.encode("utf-8"),
            salt=salt,
            n=16384,
            r=8,
            p=1,
            dklen=32,
        )
        return hmac.compare_digest(derived, expected)
    except Exception:
        return False


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_access_token(
    user_id: str,
    email: str,
    role: str = "user",
    expires_minutes: Optional[int] = None,
) -> str:
    """Create a signed HS256 JWT access token."""
    settings = get_settings()
    exp_minutes = expires_minutes or settings.app_jwt_expire_minutes
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + int(exp_minutes * 60),
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(
        settings.app_jwt_secret.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    sig_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    """Verify and decode an HS256 JWT access token. Returns None if invalid or expired."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        settings = get_settings()
        expected_sig = hmac.new(
            settings.app_jwt_secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def generate_otp_code() -> str:
    """Generate a 6-digit numeric OTP code."""
    return f"{secrets.randbelow(900000) + 100000:06d}"


def hash_otp_code(code: str) -> str:
    """Hash an OTP code using SHA-256."""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def generate_api_key() -> Tuple[str, str, str]:
    """Generate a new API key. Returns (full_key, key_prefix, key_hash)."""
    raw_token = secrets.token_urlsafe(24)
    full_key = f"ppj_live_{raw_token}"
    key_prefix = full_key[:16]
    key_hash = hash_api_key(full_key)
    return full_key, key_prefix, key_hash


def hash_api_key(full_key: str) -> str:
    """Hash an API key using SHA-256."""
    return hashlib.sha256(full_key.strip().encode("utf-8")).hexdigest()

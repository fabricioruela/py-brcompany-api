"""Masking utilities for preview tables, public SEO pages, and LGPD compliance."""

from typing import Optional


def mask_phone(area_code: Optional[str], phone: Optional[str]) -> Optional[str]:
    """Mask phone number keeping DDD and last 3-4 digits, e.g. '(11) ****-4321'."""
    if not phone or not phone.strip():
        return None
    clean_phone = "".join(ch for ch in phone if ch.isdigit())
    if not clean_phone:
        return None
    ddd = f"({area_code.strip()}) " if area_code and area_code.strip() else ""
    if len(clean_phone) >= 4:
        return f"{ddd}****-{clean_phone[-4:]}"
    return f"{ddd}****"


def mask_email(email: Optional[str]) -> Optional[str]:
    """Mask email keeping first 2 characters of local part and domain, e.g. 'co***@empresa.com.br'."""
    if not email or "@" not in email:
        return None
    local, domain = email.strip().lower().split("@", 1)
    if len(local) <= 2:
        prefix = local[:1] + "***"
    else:
        prefix = local[:2] + "***"
    return f"{prefix}@{domain}"


def mask_street(
    street_type: Optional[str],
    street: Optional[str],
    number: Optional[str],
) -> Optional[str]:
    """Mask street address for preview rows or MEI residential privacy."""
    if not street or not street.strip():
        return None
    st_type = f"{street_type.strip()} " if street_type and street_type.strip() else ""
    words = street.strip().split()
    first_word = words[0][:3] + "***" if words else "***"
    return f"{st_type}{first_word}, Nº ***"


def mask_cpf_or_tax_id(tax_id: Optional[str]) -> Optional[str]:
    """Ensure personal CPFs have middle digits masked (`***123456**`) while keeping 14-digit PJ CNPJs."""
    if not tax_id:
        return None
    cleaned = tax_id.strip()
    if "*" in cleaned:
        return cleaned
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if len(digits) == 11:
        return f"***{digits[3:9]}**"
    return cleaned

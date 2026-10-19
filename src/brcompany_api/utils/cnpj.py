"""CNPJ validation, sanitization, and formatting utilities."""

import re

_NON_DIGITS_RE = re.compile(r"\D")


def clean_cnpj(value: str) -> str:
    """Strip all non-numeric characters from a CNPJ string."""
    if not value:
        return ""
    return _NON_DIGITS_RE.sub("", value)


def format_cnpj(value: str) -> str:
    """Format an 8-digit or 14-digit CNPJ into standard Brazilian masked string."""
    digits = clean_cnpj(value)
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:14]}"
    if len(digits) == 8:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}"
    return value


def validate_cnpj(value: str) -> bool:
    """Validate a 14-digit CNPJ using official check digit verification algorithm."""
    digits = clean_cnpj(value)

    if len(digits) != 14:
        return False

    # Disallow known repeated sequences (e.g., 00000000000000, 11111111111111)
    if len(set(digits)) == 1:
        return False

    # Calculate first verification digit
    weights1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    sum1 = sum(int(digits[i]) * weights1[i] for i in range(12))
    remainder1 = sum1 % 11
    digit1 = 0 if remainder1 < 2 else 11 - remainder1

    if int(digits[12]) != digit1:
        return False

    # Calculate second verification digit
    weights2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    sum2 = sum(int(digits[i]) * weights2[i] for i in range(13))
    remainder2 = sum2 % 11
    digit2 = 0 if remainder2 < 2 else 11 - remainder2

    return int(digits[13]) == digit2

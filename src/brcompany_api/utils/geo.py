"""Geographic normalization and Brazilian UF constants."""

import unicodedata

BRAZIL_STATES = frozenset({
    "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA",
    "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN",
    "RO", "RR", "RS", "SC", "SE", "SP", "TO",
})


def normalize_place_name(value: str) -> str:
    """Normalize municipality/city names to accent-free uppercase for RFB comparison."""
    decomposed = unicodedata.normalize("NFKD", value.strip())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.upper().split())

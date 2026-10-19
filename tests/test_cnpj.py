"""Unit tests for CNPJ cleaning, formatting, and validation."""

from brcompany_api.utils.cnpj import clean_cnpj, format_cnpj, validate_cnpj


def test_clean_cnpj():
    assert clean_cnpj("00.000.000/0001-91") == "00000000000191"
    assert clean_cnpj(" 12.345.678 ") == "12345678"
    assert clean_cnpj("abc-123.xyz") == "123"
    assert clean_cnpj("") == ""


def test_format_cnpj():
    assert format_cnpj("00000000000191") == "00.000.000/0001-91"
    assert format_cnpj("12345678") == "12.345.678"
    assert format_cnpj("invalid") == "invalid"


def test_validate_cnpj():
    # Valid real CNPJs (Banco do Brasil, Petrobras, Caixa)
    assert validate_cnpj("00.000.000/0001-91") is True
    assert validate_cnpj("00000000000191") is True
    assert validate_cnpj("33.000.167/0001-01") is True
    assert validate_cnpj("00.360.305/0001-04") is True

    # Invalid check digits
    assert validate_cnpj("00.000.000/0001-92") is False
    assert validate_cnpj("33.000.167/0001-00") is False

    # Repeated sequences
    assert validate_cnpj("00000000000000") is False
    assert validate_cnpj("11111111111111") is False

    # Incorrect length
    assert validate_cnpj("12345678") is False
    assert validate_cnpj("") is False

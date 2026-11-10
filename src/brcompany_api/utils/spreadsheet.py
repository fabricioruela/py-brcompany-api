"""Spreadsheet generation utilities for CSV and native Office Open XML (.xlsx) exports."""

import csv
import io
import zipfile
from typing import Any, Dict, Iterable, List
from xml.sax.saxutils import escape

from brcompany_api.domains.company.schemas import COMPANY_SIZE_LABELS, IDENTIFIER_TYPE_LABELS
from brcompany_api.utils.cnpj import format_cnpj

EXPORT_HEADERS: List[str] = [
    "CNPJ",
    "CNPJ Básico",
    "Tipo (Matriz/Filial)",
    "Razão Social",
    "Nome Fantasia",
    "Data de Abertura",
    "CNAE Principal (Código)",
    "CNAE Principal (Descrição)",
    "CNAEs Secundários",
    "Natureza Jurídica",
    "Porte da Empresa",
    "Capital Social (R$)",
    "Optante Simples",
    "Optante MEI",
    "UF",
    "Município",
    "Bairro",
    "Logradouro",
    "Número",
    "Complemento",
    "CEP",
    "Telefone 1",
    "Telefone 2",
    "E-mail",
    "Quadro Societário (QSA)",
]


def _format_row(row: Dict[str, Any]) -> List[str]:
    """Convert a database dictionary row from company_search_index into an ordered list of strings."""
    cnpj_raw = (row.get("cnpj") or "").strip()
    cnpj_fmt = format_cnpj(cnpj_raw) if len(cnpj_raw) == 14 else cnpj_raw
    id_type = (row.get("identifier_type") or "").strip()
    id_label = IDENTIFIER_TYPE_LABELS.get(id_type, id_type or "Matriz")
    size_code = (row.get("company_size") or "00").strip()
    size_label = COMPANY_SIZE_LABELS.get(size_code, size_code)

    sec_cnaes = row.get("secondary_cnaes") or []
    if isinstance(sec_cnaes, list):
        sec_cnaes_str = ", ".join(str(c).strip() for c in sec_cnaes if c)
    else:
        sec_cnaes_str = str(sec_cnaes)

    capital = row.get("share_capital")
    capital_str = f"{float(capital):.2f}" if capital is not None else "0.00"

    simples_val = row.get("simples_optant")
    simples_str = "Sim" if simples_val in (True, "S", "s", 1) else "Não"

    mei_val = row.get("mei_optant")
    mei_str = "Sim" if mei_val in (True, "S", "s", 1) else "Não"

    ddd1 = (row.get("area_code_1") or "").strip()
    tel1 = (row.get("phone_1") or "").strip()
    phone1_full = f"({ddd1}) {tel1}".strip() if tel1 else ""

    ddd2 = (row.get("area_code_2") or "").strip()
    tel2 = (row.get("phone_2") or "").strip()
    phone2_full = f"({ddd2}) {tel2}".strip() if tel2 else ""

    street_type = (row.get("street_type") or "").strip()
    street_name = (row.get("street") or "").strip()
    full_street = f"{street_type} {street_name}".strip()

    start_date = row.get("start_activity_date")
    start_date_str = start_date.isoformat() if hasattr(start_date, "isoformat") else str(start_date or "")

    legal_nat_code = (row.get("legal_nature_code") or "").strip()
    legal_nat_desc = (row.get("legal_nature_description") or "").strip()
    legal_nat_full = f"{legal_nat_code} - {legal_nat_desc}".strip(" -")

    return [
        cnpj_fmt,
        (row.get("cnpj_base") or "").strip(),
        id_label,
        (row.get("legal_name") or "").strip(),
        (row.get("trade_name") or "").strip(),
        start_date_str,
        (row.get("main_cnae") or "").strip(),
        (row.get("main_cnae_description") or "").strip(),
        sec_cnaes_str,
        legal_nat_full,
        size_label,
        capital_str,
        simples_str,
        mei_str,
        (row.get("state") or "").strip(),
        (row.get("municipality_name") or "").strip(),
        (row.get("neighborhood") or "").strip(),
        full_street,
        (row.get("number") or "").strip(),
        (row.get("complement") or "").strip(),
        (row.get("postal_code") or "").strip(),
        phone1_full,
        phone2_full,
        (row.get("email") or "").strip().lower(),
        (row.get("partners_summary") or "").strip(),
    ]


def generate_csv_bytes(rows: Iterable[Dict[str, Any]]) -> bytes:
    """Generate UTF-8 with BOM CSV bytes (for direct compatibility with Excel in pt-BR)."""
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM for Excel
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(EXPORT_HEADERS)
    for row in rows:
        writer.writerow(_format_row(row))
    return buf.getvalue().encode("utf-8")


def _col_letter(col_idx: int) -> str:
    """Convert 1-based column index to Excel column letter (A, B, ..., Z, AA)."""
    result = ""
    while col_idx > 0:
        col_idx, rem = divmod(col_idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def generate_xlsx_bytes(rows: Iterable[Dict[str, Any]]) -> bytes:
    """Generate a valid Office Open XML (.xlsx) workbook in memory using standard library zipfile."""
    content_types_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="PanoramaPJ Leads" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>"""

    workbook_rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="10"/><name val="Calibri"/></font>
    <font><b/><sz val="10"/><name val="Calibri"/></font>
  </fonts>
  <fills count="2">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
  </fills>
  <borders count="1">
    <border><left/><right/><top/><bottom/><diagonal/></border>
  </borders>
  <cellStyleXfs count="1">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
  </cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
  </cellXfs>
</styleSheet>"""

    sheet_lines: List[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "  <sheetData>",
    ]

    # Header row (row 1, style s="1" bold)
    header_cells = []
    for col_idx, h_text in enumerate(EXPORT_HEADERS, start=1):
        ref = f"{_col_letter(col_idx)}1"
        header_cells.append(
            f'<c r="{ref}" t="inlineStr" s="1"><is><t>{escape(h_text)}</t></is></c>'
        )
    sheet_lines.append(f'    <row r="1">{"".join(header_cells)}</row>')

    # Data rows
    for row_idx, row in enumerate(rows, start=2):
        values = _format_row(row)
        row_cells = []
        for col_idx, val in enumerate(values, start=1):
            ref = f"{_col_letter(col_idx)}{row_idx}"
            safe_val = escape(val or "")
            row_cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{safe_val}</t></is></c>')
        sheet_lines.append(f'    <row r="{row_idx}">{"".join(row_cells)}</row>')

    sheet_lines.append("  </sheetData>")
    sheet_lines.append("</worksheet>")
    sheet_xml = "\n".join(sheet_lines)

    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return out_buf.getvalue()

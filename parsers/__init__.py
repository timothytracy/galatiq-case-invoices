"""Deterministic readers. Printed totals are kept even when the arithmetic is wrong."""

from pathlib import Path

from models import ExtractedInvoice
from parsers.common import _read
from parsers.csv_invoice import parse_csv_file
from parsers.json_invoice import parse_json_file
from parsers.pdf_invoice import parse_pdf_file, pdf_text
from parsers.text_invoice import parse_text_file
from parsers.xml_invoice import parse_xml_file


def read_raw(path: Path) -> str:
    """Document text only. PDF text comes from the text layer. No network."""

    if path.suffix.lower() == ".pdf":
        return pdf_text(path)
    return _read(path)


def parse_file(path: Path) -> ExtractedInvoice:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_json_file(path)
    if suffix == ".csv":
        return parse_csv_file(path)
    if suffix == ".xml":
        return parse_xml_file(path)
    if suffix == ".pdf":
        return parse_pdf_file(path)
    if suffix == ".txt":
        return parse_text_file(path)
    raise ValueError(f"unsupported invoice type: {suffix or path.name}")

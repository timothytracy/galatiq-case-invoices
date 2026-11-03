"""Deterministic readers. Printed totals are kept even when the arithmetic is wrong."""

import json
from pathlib import Path
from xml.etree import ElementTree

from models import ExtractedInvoice
from parsers.common import _read
from parsers.csv_invoice import parse_csv_file
from parsers.json_invoice import parse_json_file
from parsers.pdf_invoice import parse_pdf_file, pdf_text
from parsers.text_invoice import parse_text_file
from parsers.xml_invoice import parse_xml_file

SUFFIXES = frozenset({".txt", ".pdf", ".json", ".csv", ".xml"})


def read_raw(path: Path) -> str:
    """Document text only. PDF text comes from the text layer. No network."""

    if path.suffix.lower() == ".pdf":
        return pdf_text(path)
    return _read(path)


def unreadable_reason(path: Path, raw: str) -> str | None:
    """Why this file cannot be an invoice, before any model reads it. None when it can.

    An empty page gives a model nothing to read, so it would invent an
    invoice. A JSON or XML file that does not parse was cut off or damaged;
    a model would read the fragment and the reject would name the wrong cause.
    """

    if not raw.strip():
        return "The file is empty."
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return f"The JSON is cut off or invalid at line {exc.lineno} column {exc.colno}: {exc.msg}."
        if not isinstance(data, dict):
            return "The JSON is not an invoice object."
    if suffix == ".xml":
        try:
            ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            line, column = exc.position
            return f"The XML is cut off or invalid at line {line} column {column + 1}."
    if suffix == ".csv" and len([line for line in raw.splitlines() if line.strip(" ,\t")]) < 2:
        return "The CSV has a header but no invoice rows."
    return None


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

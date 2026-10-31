"""Text-layer PDFs. The same page text the graph reads during ingestion."""

from pathlib import Path

from models import ExtractedInvoice
from parsers.common import _source
from parsers.text_invoice import parse_text


def pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF parsing needs the pypdf package") from exc
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_pdf_file(path: Path) -> ExtractedInvoice:
    raw = pdf_text(path)
    return parse_text(raw, _source(path, "pdf", raw))

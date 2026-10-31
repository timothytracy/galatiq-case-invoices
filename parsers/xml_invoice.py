"""XML invoices. Tags may be namespaced; the local name is what is matched."""

from pathlib import Path
from xml.etree import ElementTree

from models import ExtractedInvoice
from parsers.common import _blank, _build, _make_item, _money, _rate, _read, _source


def parse_xml_file(path: Path) -> ExtractedInvoice:
    raw = _read(path)
    root = ElementTree.fromstring(raw)
    items = []
    for node in root.iter():
        if _xml_tag(node) != "item":
            continue
        name = _xml_child(node, "name") or _xml_child(node, "description") or ""
        quantity = _xml_child(node, "quantity")
        if not name or quantity is None:
            continue
        items.append(
            _make_item(
                name,
                quantity,
                _xml_child(node, "unit_price"),
                _xml_child(node, "amount") or _xml_child(node, "line_total"),
                _xml_child(node, "note"),
            )
        )
    return _build(
        vendor=_blank(_xml_first(root, "vendor")),
        invoice_number_raw=_blank(_xml_first(root, "invoice_number")),
        invoice_date=_blank(_xml_first(root, "date")),
        due_date=_blank(_xml_first(root, "due_date")),
        currency=_blank(_xml_first(root, "currency")) or "USD",
        line_items=items,
        subtotal=_money(_xml_first(root, "subtotal")),
        tax_rate=_rate(_xml_first(root, "tax_rate")),
        tax_amount=_money(_xml_first(root, "tax_amount")),
        shipping=_money(_xml_first(root, "shipping")),
        total=_money(_xml_first(root, "total")),
        payment_terms=_blank(_xml_first(root, "payment_terms")),
        notes=_blank(_xml_first(root, "notes")),
        source=_source(path, "xml", raw),
    )


def _xml_tag(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _xml_child(node, name: str) -> str | None:
    for child in list(node):
        if _xml_tag(child) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _xml_first(root, name: str) -> str | None:
    for node in root.iter():
        if _xml_tag(node) == name and node.text and node.text.strip():
            return node.text.strip()
    return None

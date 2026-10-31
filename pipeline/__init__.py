"""One function per graph node. graph.py only wires them.

Hard rules live in rules.py. Extraction, approval wording, critique, and the
audit verdict are model calls. A failed model call becomes manual_review and
is not paid.
"""

from pipeline.approval import approve
from pipeline.audit import audit
from pipeline.critique import critique
from pipeline.ingestion import ingest
from pipeline.payment import payment
from pipeline.reject_log import reject_log
from pipeline.validation import validate

__all__ = [
    "approve",
    "audit",
    "critique",
    "ingest",
    "payment",
    "reject_log",
    "validate",
]

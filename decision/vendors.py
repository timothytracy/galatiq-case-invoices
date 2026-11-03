"""The vendor master. A vendor not on it is a NEW_VENDOR hold."""

import json
import os
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_MASTER = ROOT / "data" / "vendor_master.json"
# Legal-form words that extraction may add or drop. "Co" also covers "Company".
_SUFFIXES = {"inc", "incorporated", "ltd", "limited", "llc", "co", "company", "corp", "corporation"}


def vendor_name_key(name: str | None) -> str:
    """Casefold, & as and, punctuation removed, trailing legal-form words dropped."""

    text = (name or "").casefold().replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)


def master_path() -> Path:
    """VENDOR_MASTER, when set, replaces data/vendor_master.json. Read on each call."""

    override = os.environ.get("VENDOR_MASTER", "").strip()
    return Path(override) if override else VENDOR_MASTER


@lru_cache(maxsize=4)
def _keys(path: str, stamp: float) -> frozenset[str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = data["vendors"] if isinstance(data, dict) else data
    return frozenset(key for key in map(vendor_name_key, names) if key)


def known_vendor(name: str | None) -> bool:
    """True when the vendor is on the vendor master. A missing file fails closed."""

    key = vendor_name_key(name)
    if not key:
        return False
    path = master_path()
    try:
        return key in _keys(str(path), path.stat().st_mtime)
    except (OSError, ValueError, KeyError, TypeError):
        return False

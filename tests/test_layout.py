"""Production code does not import test helpers."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"tests", ".venv", "venv", ".git"}


def test_no_module_outside_tests_imports_tests():
    pattern = re.compile(r"^\s*(from tests[\s.]|import tests\b)", re.MULTILINE)
    offenders = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*.py")
        if not SKIP & set(path.relative_to(ROOT).parts)
        and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []

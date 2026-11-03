"""Amounts read straight from the invoice text: the printed total, the amount
in words, and a stated balance or amount due. Reading only, no decision."""

import re
from decimal import Decimal, InvalidOperation

_NUMBER = r"-?[0-9][0-9,]*(?:\.[0-9]+)?"
# "Total:", "Total Amount:", "Grand Total:", "Total Due:", or a "total" JSON/XML key.
# Not "Subtotal". Not "Total Amount in words".
_TOTAL = re.compile(
    rf"""(?imx)(?<![a-z_])["'<]?(?:grand[\s_]+)?total(?:[\s_]+(?:amount|due))?["'>]?
        \s*[:=>]?\s*["']?\s*\$?\s*({_NUMBER})"""
)
_WORDS = re.compile(
    r"""(?imx)(?:amount|total)[\s_]+in[\s_]+words["'>]?\s*[:=>]\s*["']?([^\n"'<]+)"""
)
# An amount the page says is owed. The separator is optional, as on a total line,
# and a CSV row may use a comma. Not a previous, prior, account, or credit
# balance, and not a balance brought forward. A trailing "credit" is not owed.
# "Total Due" and "Please remit" are amounts owed, not only a second total.
_BALANCE = re.compile(
    rf"""(?imx)
    (?<![a-z_])
    (?<!previous[\s_])(?<!prior[\s_])
    (?<!account[\s_])(?<!credit[\s_])
    (?:
        balance(?![\s_]+forward)(?:[\s_]+(?:due|owing|payable))?
        | amount[\s_]+(?:due|payable)
        | remaining[\s_]+to[\s_]+pay
        | total[\s_]+due
        | please[\s_]+remit
    )
    (?:\s*\([^)\n]*\))?
    ["'>]?
    \s*[:,=>]?
    \s*(?:usd\s+)?
    ["']?
    \s*\$?
    \s*({_NUMBER})
    """
)
_TRAILING_CREDIT = re.compile(r"(?i)\s*credit\b")

_SMALL = {
    word: value
    for value, word in enumerate(
        (
            "zero one two three four five six seven eight nine ten eleven twelve thirteen "
            "fourteen fifteen sixteen seventeen eighteen nineteen"
        ).split()
    )
}
_TENS = {
    word: value * 10
    for value, word in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)
}
_SCALES = {"thousand": 1000, "million": 1_000_000}
_SKIP = {"and", "a", "only", "exactly", "us", "usd", "no"}


def _money(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _amounts(pattern: re.Pattern, raw_text: str | None) -> list[Decimal]:
    found = []
    for match in pattern.finditer(raw_text or ""):
        value = _money(match.group(1))
        if value is not None:
            found.append(value)
    return found


def printed_totals(raw_text: str | None) -> list[Decimal]:
    """Every amount printed on a total line, in page order."""

    return _amounts(_TOTAL, raw_text)


def balances_due(raw_text: str | None) -> list[Decimal]:
    """Every amount the page says is owed. A trailing credit is not included."""

    text = raw_text or ""
    found = []
    for match in _BALANCE.finditer(text):
        if _TRAILING_CREDIT.match(text[match.end():]):
            continue
        value = _money(match.group(1))
        if value is not None:
            found.append(value)
    return found


def _whole(words: list[str]) -> int | None:
    total = 0
    current = 0
    seen = False
    for word in words:
        if word in _SKIP:
            continue
        if word in _SMALL:
            current += _SMALL[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in _SCALES:
            total += (current or 1) * _SCALES[word]
            current = 0
        else:
            return None
        seen = True
    return total + current if seen else None


def words_to_amount(text: str | None) -> Decimal | None:
    """'Ten thousand two hundred fifty dollars and 00/100' -> 10250.00.

    None when any word is not a number word, so an odd phrase is never guessed at.
    """

    if not text:
        return None
    clean = re.sub(r"[-,.]", " ", text.lower()).strip()
    cents = Decimal("0")
    fraction = re.search(r"\b(\d{1,2})\s*/\s*100\b", clean)
    if fraction:
        cents = Decimal(fraction.group(1)) / 100
        clean = clean[: fraction.start()] + " " + clean[fraction.end():]
    parts = re.split(r"\bdollars?\b", clean, maxsplit=1)
    head, tail = (parts[0], parts[1]) if len(parts) == 2 else (clean, "")
    tail_words = tail.split()
    if tail_words and tail_words[-1] in {"cent", "cents"}:
        if fraction:
            return None
        spoken = _whole(tail_words[:-1])
        if spoken is None or spoken > 99:
            return None
        cents = Decimal(spoken) / 100
    elif any(word not in _SKIP for word in tail_words):
        return None
    dollars = _whole(head.split())
    if dollars is None:
        return None
    return Decimal(dollars) + cents


def amount_in_words(raw_text: str | None) -> Decimal | None:
    """The first readable amount-in-words line, or None."""

    for match in _WORDS.finditer(raw_text or ""):
        value = words_to_amount(match.group(1))
        if value is not None:
            return value
    return None

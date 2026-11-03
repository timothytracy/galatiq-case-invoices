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
# An amount the page says is owed. A plain "Account balance:" line is not.
# "Account Balance Due" still is. "credit" or "CR" marks a credit only when it
# sits against the amount, not in "Credit card".
_LABEL = re.compile(
    r"""(?ix)
    (?<![a-z_])
    (?<!previous[ \t_])(?<!prior[ \t_])(?<!credit[ \t_])
    (?:
        balance(?:[ \t_]+|\n[ \t]*)(?:due|owing|payable|outstanding)
        | (?<!account[ \t_])balance(?![ \t_]+forward)
        | amount[ \t_]*due
        | amount\n[ \t]*due
        | amount(?:[ \t_]+|\n[ \t]*)owing
        | amount(?:[ \t_]+|\n[ \t]*)(?:payable|to[ \t_]+pay)
        | remaining(?:[ \t_]+|\n[ \t]*)(?:to[ \t_]+pay|balance)
        | total(?:[ \t_]+|\n[ \t]*)due
        | please[ \t_]+remit
    )
    """
)
# Right next to the amount: "$40.00 credit" or "CR". Not "Credit card".
_TRAILING_CREDIT = re.compile(r"(?i)\s*(?:credit\b(?!\s+card\b)|cr\b)")
_AFTER_COUNT = re.compile(r"(?i)\s*(?:days?|shipments?|items?|units?|of)\b")
_SAME_LINE = re.compile(
    rf"""(?ix)
    [ \t]*
    (?:\([^)\n]{{0,40}}\)[ \t]*)?
    ["'>]*[ \t]*
    (?:[:,=>]|-|\.{{2,}})?[ \t]*
    ["'>]*[ \t]*
    (?:(?:usd|us\$)[ \t]*)?
    \$?[ \t]*
    (?P<num>{_NUMBER})
    """
)
_NEXT_LINE_MONEY = re.compile(
    rf"""(?ix)
    [ \t]*\n[ \t]*
    (?:(?:usd|us\$)[ \t]*)?
    \$?[ \t]*
    (?P<num>{_NUMBER})
    """
)

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


def _money_shaped(gap: str, number: str) -> bool:
    """A currency sign or code, or cents. A bare integer is not money."""

    return "$" in gap or "." in number or re.search(r"(?i)usd|us\$", gap) is not None


def _separator(gap: str) -> bool:
    return re.search(r"[:,=>]|-|\.{2,}", gap) is not None


def _accept(gap: str, number: str, tail: str) -> Decimal | None:
    if _TRAILING_CREDIT.match(tail) or _AFTER_COUNT.match(tail):
        return None
    if not (_money_shaped(gap, number) or _separator(gap)):
        return None
    return _money(number)


def balances_due(raw_text: str | None) -> list[Decimal]:
    """Every amount the page says is owed.

    The gap between the label and the number stays on one line, unless the
    number looks like money. A bare count (days, shipments, "2 of 6") is not
    an amount owed. A credit marker sitting on the amount is not either.
    """

    text = raw_text or ""
    found = []
    for label in _LABEL.finditer(text):
        rest = text[label.end():]
        same = _SAME_LINE.match(rest)
        if same:
            value = _accept(same.group(0)[: same.start("num")], same.group("num"), rest[same.end():])
            if value is not None:
                found.append(value)
            continue
        nxt = _NEXT_LINE_MONEY.match(rest)
        if nxt is None:
            continue
        gap = nxt.group(0)[: nxt.start("num")]
        number = nxt.group("num")
        if not _money_shaped(gap, number):
            continue
        value = _accept(gap, number, rest[nxt.end():])
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

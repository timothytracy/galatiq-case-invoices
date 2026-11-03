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
        balancedue
        | balance(?:[ \t_]+|\n[ \t]*)(?:due|owing|payable|outstanding|remaining)
        | (?<!account[ \t_])(?<!opening[ \t_])(?<!deposit[ \t_])(?<!statement[ \t_])
            balance(?![ \t_]+forward)
        | amount[ \t_]*due
        | amount\n[ \t]*due
        | amount(?:[ \t_]+|\n[ \t]*)(?:owing|remaining)
        | amount(?:[ \t_]+|\n[ \t]*)(?:payable|to[ \t_]+pay)
        | remaining(?:[ \t_]+|\n[ \t]*)(?:to[ \t_]+pay|balance)
        | total(?:[ \t_]+|\n[ \t]*)due
        | net[ \t_]+due
        | now[ \t_]+due
        | pay[ \t_]+this[ \t_]+amount
        | outstanding
        | please[ \t_]+remit(?:[ \t_]+the[ \t_]+balance[ \t_]+of)?
        | (?<![a-z_])remit
    )
    """
)
# "$40.00 credit" or "CR", and nothing after it. "Credit card", "Credit cards",
# and "Credit or debit" are phrases, not markers.
_TRAILING_CREDIT = re.compile(r"(?i)[ \t]*(?:credit\b(?![ \t]+[A-Za-z])|cr\b)")
# "2 of 6", "30 days". Not "$8,000.00 of ..." or "$8,000.00 items".
_AFTER_COUNT = re.compile(r"(?i)[ \t]*(?:days?|shipments?|items?|units?|of)\b")
_GLUED = re.compile(r"(?i)(?:%|st\b|nd\b|rd\b|th\b|/|-)")
_FOLLOWING_WORD = re.compile(r"[ \t]+[A-Za-z]")
_SAME_LINE = re.compile(
    rf"""(?ix)
    [ \t]*
    (?:\([^)\n]{{0,40}}\)[ \t]*)?
    ["'>]*[ \t]*
    (?:[:,=>]|\.{{2,}}|-[ \t]+)?[ \t]*
    ["'>]*[ \t]*
    (?:(?:usd|us\$)[ \t]*)?
    \$?[ \t]*
    (?P<num>{_NUMBER})
    """
)
# One blank line is still the next line. A glued dash (BALANCE-2026) is not.
_NEXT_LINE_MONEY = re.compile(
    rf"""(?ix)
    [ \t]*
    (?:[:,=>][ \t]*)?
    (?:\n[ \t]*){{1,2}}
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
    return re.search(r"[:,=>]|\.{2,}|-[ \t]", gap) is not None


def _bare_integer(gap: str, number: str) -> bool:
    return "." not in number and not _money_shaped(gap, number)


def _accept(gap: str, number: str, tail: str) -> Decimal | None:
    if _TRAILING_CREDIT.match(tail):
        return None
    # "of" / "items" / "days" drop a bare integer. A dollar amount stays.
    if _bare_integer(gap, number) and (
        _AFTER_COUNT.match(tail) or _GLUED.match(tail) or _FOLLOWING_WORD.match(tail)
    ):
        return None
    if _money_shaped(gap, number) or _separator(gap) or re.fullmatch(r"[ \t]*", gap):
        return _money(number)
    return None


def balances_due(raw_text: str | None) -> list[Decimal]:
    """Every amount the page says is owed.

    A blank line may sit between the label and a money amount. A bare integer
    right after the label counts. "of" or "items" drops that integer only.
    "credit" alone, or "CR", marks a credit. "Credit card" and "Credit or
    debit" do not. A dash glued to a reference number is not a separator.
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

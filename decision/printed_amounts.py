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
# Space, tab, or a non-breaking space. Newlines are written out where a label
# may split. A lookbehind stays one character wide.
_SP = r"[ \t\u00a0\u202f_]"
# An amount the page says is owed. A plain "Account balance:" is not.
# "Account Balance Due" still is. "credit" or "CR" marks a credit only when
# nothing follows it. "Credit card" and "Credit or debit" do not.
_LABEL = re.compile(
    rf"""(?ix)
    (?<![a-z_])
    (?<!previous{_SP})(?<!prior{_SP})(?<!credit{_SP})
    (?:
        balancedue
        | balance(?:{_SP}+|\n[ \t\u00a0]*)+(?:due|owing|payable|outstanding|remaining)
        | (?<!account{_SP})(?<!opening{_SP})(?<!deposit{_SP})(?<!statement{_SP})
            balance(?!{_SP}+forward)
        | amount{_SP}*due
        | amount(?:{_SP}+|\n[ \t\u00a0]*)+(?:owing|remaining|outstanding)
        | amount(?:{_SP}+|\n[ \t\u00a0]*)+(?:payable|to{_SP}+pay)
        | remaining(?:{_SP}+|\n[ \t\u00a0]*)+(?:to{_SP}+pay|balance)
        | total(?:{_SP}+|\n[ \t\u00a0]*)+due
        | net{_SP}+due
        | now{_SP}+due
        | pay{_SP}+this{_SP}+amount
        | outstanding
        | please{_SP}+remit(?:{_SP}+the{_SP}+balance{_SP}+of)?
        | (?<![a-z_])remit
    )
    """
)
_TRAILING_CREDIT = re.compile(r"(?i)[ \t\u00a0]*(?:credit\b(?![ \t\u00a0]+[A-Za-z])|cr\b)")
_AFTER_COUNT = re.compile(r"(?i)[ \t\u00a0]*(?:days?|shipments?|items?|units?)\b")
# "8000 of 10500" at the end of the line. "2 of 6" and "1 of 2 shipments" are counts.
_OF_TOTAL = re.compile(
    r"(?i)[ \t\u00a0]*of[ \t\u00a0]+\$?[ \t\u00a0]*([0-9][0-9,]*(?:\.[0-9]+)?)"
    r"[ \t\u00a0]*(?:usd|dollars?)?[ \t\u00a0]*(?:\n|$)"
)
# A dash glued on, as in "30-Oct" or "4500-B". A spaced dash is still a separator.
_GLUED = re.compile(r"(?i)(?:%|st\b|nd\b|rd\b|th\b|/|-)")
_FOLLOWING_WORD = re.compile(r"[ \t\u00a0]+(?!usd\b|us\$|dollars?\b)[A-Za-z]")
_CURRENCY_TAIL = re.compile(r"(?i)[ \t\u00a0]*(?:usd|us\$|dollars?)\b")
_EU = r"[0-9]{1,3}(?:\.[0-9]{3})+,[0-9]{2}"
_SAME_LINE = re.compile(
    rf"""(?ix)
    [ \t\u00a0]*
    (?:\([^)\n]{{0,40}}\)[ \t\u00a0]*)?
    (?:[ \t\u00a0]+[A-Za-z_]+=\"[^\"\n]*\")*[ \t\u00a0]*
    ["'>]*[ \t\u00a0]*
    (?:[:,=>]|\.{{2,}}|-[ \t\u00a0]+)?[ \t\u00a0]*
    ["'>]*[ \t\u00a0]*
    (?:(?:usd|us\$)[ \t\u00a0]*)?
    \$?[ \t\u00a0]*
    (?P<num>{_EU}|{_NUMBER})
    """
)
# Any run of blank lines, or one short parenthetical note, then the amount.
_NEXT_LINE_MONEY = re.compile(
    rf"""(?ix)
    [ \t\u00a0]*
    (?:[:,=>][ \t\u00a0]*)?
    (?:
        (?:\n[ \t\u00a0]*){{1,8}}
        | \n[ \t\u00a0]*\([^)\n]{{0,60}}\)[ \t\u00a0]*\n[ \t\u00a0]*
    )
    (?:(?:usd|us\$)[ \t\u00a0]*)?
    \$?[ \t\u00a0]*
    (?P<num>{_EU}|{_NUMBER})
    """
)
_DEPOSIT = re.compile(
    r"(?im)^\s*(?:less\b[^\n]*|deposit (?:received|paid|applied)[^\n]*"
    r"|credit applied[^\n]*|payments? received[^\n]*|amount paid[^\n]*"
    r"|paid to date[^\n]*)[:\s]\s*(?:-\s?\$|\(\$|\$\s?-|-)\s?[0-9][0-9,]*\.[0-9]{2}"
)
_DEPOSIT_AMOUNT = re.compile(r"-?\$?\s?-?[0-9][0-9,]*\.[0-9]{2}")

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
    """US '8,000.00', European '8.000,00', or '-$2,500.00'."""

    try:
        if re.fullmatch(_EU, text):
            return Decimal(text.replace(".", "").replace(",", "."))
        cleaned = text.replace(",", "").replace("$", "").replace(" ", "")
        return Decimal(cleaned)
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


def _money_shaped(gap: str, number: str, tail: str = "") -> bool:
    """A currency sign, code, or word, or cents. A bare integer is not money."""

    if "$" in gap or re.search(r"(?i)usd|us\$", gap):
        return True
    if _CURRENCY_TAIL.match(tail):
        return True
    return "." in number and not tail.startswith(".")


def _separator(gap: str) -> bool:
    return re.search(r"[:,=>]|\.{2,}|-[ \t\u00a0]", gap) is not None


def _bare_integer(gap: str, number: str, tail: str) -> bool:
    return "." not in number and not _money_shaped(gap, number, tail)


def _year_id(label: str, number: str) -> bool:
    """'BALANCE 2026' or 'REMIT2026'. Not 'Balance Due 8000'."""

    if not re.fullmatch(r"20\d{2}|19\d{2}", number):
        return False
    word = label.rsplit(None, 1)[-1]
    return word.lower() in {"balance", "remit", "outstanding", "due"}


def _street_follows(gap: str, tail: str) -> bool:
    """'Remit: 1200' then 'Commerce Blvd'. The number shares the label's line."""

    if "\n" in gap:
        return False
    return (
        re.match(
            r"(?i)\n[ \t\u00a0]*[A-Za-z].*\b(?:blvd|boulevard|way|street|st|road|rd|"
            r"ave|avenue|drive|dr|lane|ln|court|ct|suite|ste|pkwy|parkway|box)\b",
            tail,
        )
        is not None
    )


def _accept(gap: str, number: str, tail: str, label: str = "") -> Decimal | None:
    if gap == "" or _TRAILING_CREDIT.match(tail):
        return None
    if tail.startswith("%") or ( "." in number and re.match(r"\.\d", tail)):
        return None
    of_total = _OF_TOTAL.match(tail)
    if of_total:
        other = _money(of_total.group(1))
        if other is not None and other >= 100:
            return _money(number)
    bare = _bare_integer(gap, number, tail)
    if bare and (
        _AFTER_COUNT.match(tail)
        or _GLUED.match(tail)
        or _FOLLOWING_WORD.match(tail)
        or _street_follows(gap, tail)
        or _year_id(label, number)
    ):
        return None
    own_line = re.fullmatch(r"[\s\u00a0:=,>\-]*", gap) is not None and tail.split("\n", 1)[0].strip() == ""
    if _money_shaped(gap, number, tail) or _separator(gap) or own_line:
        return _money(number)
    return None


def _cells(line: str) -> list[tuple[int, str]]:
    """Cells split by two or more spaces. One space stays inside a cell."""

    return [(match.start(), match.group(0)) for match in re.finditer(r"\S(?:\S| (?! ))*", line)]


def _column_amounts(text: str) -> tuple[list[Decimal], set[int]]:
    """Amount under a label when both lines have the same number of cells.

    Returns those amounts and the line indexes consumed by the table, so a
    later scan does not also read the neighbouring cell.
    """

    found: list[Decimal] = []
    used: set[int] = set()
    lines = text.split("\n")
    for index, line in enumerate(lines):
        header = _cells(line)
        if len(header) < 2:
            continue
        value_at = index + 1
        while value_at < len(lines) and not lines[value_at].strip():
            value_at += 1
        if value_at >= len(lines):
            continue
        values = _cells(lines[value_at])
        if len(values) != len(header):
            continue
        matched = False
        for place, (start, cell) in enumerate(header):
            if _LABEL.fullmatch(cell.strip(" :")) is None:
                continue
            value_start, value = values[place]
            if abs(value_start - start) > 6:
                continue
            amount = _money(re.sub(r"[^0-9,.\-]", "", value))
            if amount is None or not re.search(r"\d", value):
                continue
            found.append(amount)
            matched = True
        if matched:
            used.add(index)
            used.add(value_at)
    return found, used


def _deposit_balance(text: str, owed: list[Decimal]) -> Decimal | None:
    """Implied balance when a negative deposit or credit sits under the total."""

    if owed:
        return None
    totals = list(_TOTAL.finditer(text))
    if not totals:
        return None
    billed = max((_money(match.group(1)) or Decimal("0")) for match in totals)
    match = _DEPOSIT.search(text[totals[-1].end():])
    if match is None:
        return None
    amount = _DEPOSIT_AMOUNT.search(match.group(0))
    if amount is None:
        return None
    paid = abs(_money(amount.group(0)) or Decimal("0"))
    implied = billed - paid
    if implied > 0 and implied < billed:
        return implied
    return None


def balances_due(raw_text: str | None) -> list[Decimal]:
    """Every amount the page says is owed.

    A currency word may follow the number. "8000 of 10500" counts. Blank
    lines, or one short note, may sit between the label and the amount.
    A number glued to a label, a dotted date, and a percent do not count.
    Two or more spaces line up a table. A negative deposit with no owed
    line is the amount already paid.
    """

    text = raw_text or ""
    columns, table_lines = _column_amounts(text)
    found = list(columns)
    for label in _LABEL.finditer(text):
        line_no = text.count("\n", 0, label.start())
        if line_no in table_lines:
            continue
        rest = text[label.end():]
        same = _SAME_LINE.match(rest)
        if same:
            value = _accept(
                same.group(0)[: same.start("num")],
                same.group("num"),
                rest[same.end():],
                label.group(0),
            )
            if value is not None:
                found.append(value)
            continue
        nxt = _NEXT_LINE_MONEY.match(rest)
        if nxt is None:
            continue
        value = _accept(
            nxt.group(0)[: nxt.start("num")],
            nxt.group("num"),
            rest[nxt.end():],
            label.group(0),
        )
        if value is not None:
            found.append(value)
    deposit = _deposit_balance(text, found)
    if deposit is not None:
        found.append(deposit)
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

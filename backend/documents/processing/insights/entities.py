"""Typed facts found by rule: dates, deadlines, amounts, contacts, references.

Rules rather than a model, on purpose: every type here has a shape a regular
expression describes better than a statistical tagger trained on news text
does - an Indian landline, "Rs. 1,20,000/-", "TR/2024/117", "on or before
15th March 2025" - and a rule never invents an entity that is not in the
text. Each match keeps its span; normalisation (ISO dates, amounts in one
currency format, E.164 phones) is added only when it is unambiguous, and is
otherwise left empty rather than guessed.

Numeric dates are read DAY FIRST (12/03/2024 is 12 March), the convention of
the documents this platform holds; a first number above 12 can only be a
day, a second number above 12 only a day, and that is used when present.
"""

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MAX_ENTITIES = 200
LABEL_WORDS = 12

MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
MONTH = (
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|"
    r"july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
IC = re.IGNORECASE

DATE_PATTERNS = (
    # 12/03/2024, 12-03-24, 12.03.2024
    re.compile(
        r"(?<![\w/.-])(?P<a>\d{1,2})(?P<sep>[./-])(?P<b>\d{1,2})(?P=sep)"
        r"(?P<year>\d{4}|\d{2})(?![\w/-]|\.\d)"
    ),
    # 2024-03-12
    re.compile(r"(?<![\w/.-])(?P<year>\d{4})-(?P<b>\d{2})-(?P<a>\d{2})(?![\w/-])"),
    # 12th March 2024, 12 Mar. 2024, 12 of March, 2024
    re.compile(
        r"\b(?P<a>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + MONTH + r"\.?,?\s+"
        r"(?P<year>\d{4})\b",
        IC,
    ),
    # March 12, 2024 / March 12th 2024
    re.compile(
        r"\b" + MONTH + r"\.?\s+(?P<a>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b",
        IC,
    ),
    # March 2024 (a month, normalised to YYYY-MM, never to a day)
    re.compile(r"\b" + MONTH + r"\.?,?\s+(?P<year>(?:19|20)\d{2})\b", IC),
)

DEADLINE_BEFORE = re.compile(
    r"(?:last\s+date|due\s+date|closing\s+date|deadline|due(?:\s+(?:on|by))?|"
    r"not\s+later\s+than|on\s+or\s+before|latest\s+by|closes?\s+on|before|"
    r"till|until|up\s*to|by)"
    r"(?:\s+(?:for|of)\s+[^.;:\n]{1,80}?)?"
    r"\s*(?:is|are|will\s+be|shall\s+be)?\s*(?:[:\-]\s*)?(?:the\s+)?$",
    IC,
)
DEADLINE_AFTER = re.compile(
    r"^\s*(?:is|will\s+be|shall\s+be)\s+the\s+(?:last|closing|due)\s+date", IC
)
DEADLINE_LABEL = re.compile(
    r"\b((?:last|due|closing)\s+date|deadline)\s+(?:for|of|to)\s+"
    r"(?P<what>[^.;:\n]{3,80}?)(?=\s+(?:is|are|will|shall|on|before|by)\b|[,:;\-(]|$)",
    IC,
)

MONEY_PATTERNS = (
    (
        re.compile(
            r"(?:₹|\bRs\.?|\bINR\b|\brupees\b)\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)"
            r"(?:\s*/-)?(?:\s*(?P<scale>lakhs?|lacs?|crores?|thousand)\b)?",
            IC,
        ),
        "INR",
    ),
    (
        re.compile(
            r"\b(?P<amount>\d[\d,]*(?:\.\d{1,2})?)\s*(?P<scale>lakhs?|lacs?|crores?)?\s*"
            r"(?:rupees|rs\.?|inr)(?![a-z])",
            IC,
        ),
        "INR",
    ),
    (
        re.compile(r"(?:\bUS\$|\$|\bUSD\s*)(?P<amount>\d[\d,]*(?:\.\d{1,2})?)", IC),
        "USD",
    ),
    (
        re.compile(r"\b(?P<amount>\d[\d,]*(?:\.\d{1,2})?)\s*(?:USD|dollars)\b", IC),
        "USD",
    ),
)
SCALES = {"lakh": 100_000, "lac": 100_000, "crore": 10_000_000, "thousand": 1_000}

PERCENT = re.compile(
    r"\b(?P<value>\d{1,3}(?:\.\d+)?)\s*(?:%|per\s*cent\b|percent\b)", IC
)

NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty-five": 45,
    "sixty": 60,
    "ninety": 90,
}
DURATION = re.compile(
    r"\b(?P<count>\d{1,3}|"
    + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
    + r")"
    r"[\s-]+(?P<unit>(?:working\s+|calendar\s+)?days?|weeks?|months?|years?|"
    r"hours?|hrs?|minutes?|mins?)\b",
    IC,
)
DURATION_CODES = {
    "day": ("D", False),
    "week": ("W", False),
    "month": ("M", False),
    "year": ("Y", False),
    "hour": ("H", True),
    "hr": ("H", True),
    "minute": ("M", True),
    "min": ("M", True),
}

EMAIL = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)
URL = re.compile(r"\b(?:https?://|www\.)[^\s<>\"'()\[\]{}]+", IC)

# (pattern, needs_cue). A number that announces itself as a phone number -
# a country code, a toll-free prefix - is one. A bare ten digits starting
# with 9 is just as often a roll number or two amounts in a table row
# ("85000 65000"), so those count only after a word like "Phone" or "Mobile".
PHONE_PATTERNS = (
    # +91 98765 43210, +44 20 7946 0958, 0091-...
    (
        re.compile(
            r"(?<![\w+])(?:\+|00)\d{1,3}[ .-]?(?:\(\d{1,5}\)[ .-]?)?\d(?:[ .-]?\d){6,12}(?!\w)"
        ),
        False,
    ),
    # Toll free: 1800-123-4567
    (re.compile(r"(?<![\w./-])1800[ -]\d{3}[ -]\d{4}(?![\w/-])"), False),
    # Indian mobile: 98765 43210, 98765-43210, 9876543210, 09876543210
    (re.compile(r"(?<![\w./-])0?[6-9]\d{4}[ -]?\d{5}(?![\w/-]|\.\d)"), True),
    (re.compile(r"(?<![\w./-])[6-9]\d{2}[ -]\d{3}[ -]\d{4}(?![\w/-]|\.\d)"), True),
    # Landline with STD code: 040-2345 6789, (040) 23456789
    (
        re.compile(r"(?<![\w./-])\(?0\d{2,4}\)?[ -]\d{3,4}[ -]?\d{3,4}(?![\w/-]|\.\d)"),
        True,
    ),
)
PHONE_CUE = re.compile(
    r"\b(?:ph(?:one)?|mob(?:ile)?|tel(?:ephone)?|cell|contact|call|whats\s*app|"
    r"helpline|fax|landline)\b[^\n]{0,40}$",
    IC,
)
PHONE_REFUSE_BEFORE = re.compile(
    r"\b(?:no|nos|roll|reg(?:istration)?|id|account|a/c|ref|receipt|order|"
    r"circular|file|isbn|pin|code)\b\.?\s*(?:no\.?)?\s*[:#-]?\s*$",
    IC,
)

REFERENCE_LABELLED = re.compile(
    r"\b(?:ref(?:erence)?|circular|order|memo|letter|file|proceedings?|"
    r"notification|g\.?\s?o\.?(?:\s?\(?ms\)?)?|lr|f)\.?\s*(?:no\.?|number|#)\s*"
    r"[:.\-]?\s*(?P<value>[A-Z0-9][A-Z0-9./()\-]{2,40})",
    IC,
)
REFERENCE_SLASHED = re.compile(r"\b[A-Z]{1,6}(?:[./-][A-Z0-9]{1,10}){1,5}\b")
REGULATION_CODE = re.compile(r"\b(?P<value>R\d{2})\b")
REGULATION_WORD = re.compile(r"regulation", IC)

COURSE_CODE = re.compile(
    r"\b(?P<letters>[A-Z]{2,4})\s?(?P<digits>\d{3,4})(?P<suffix>[A-Z]?)\b"
)
COURSE_LABELLED = re.compile(
    r"course\s*code\s*[:\-]?\s*(?P<value>[A-Z]{2,4}\s?\d{3,4}[A-Z]?)", IC
)
COURSE_CODE_REFUSED = frozenset(
    "INR USD RS ISO FY AY AD BC NO PIN COVID SEC PH TR REF NH PO AM PM GO PAN "
    "GST TDS CIN DIN IFSC ATM OTP AC DC KM KG".split()
)

# Words of a name are separated by spaces only: a name never continues
# across a line break, and "Office of the Registrar<newline><newline>Clause"
# is a name and a heading, not one long name.
ORG_PATTERNS = (
    re.compile(
        r"\b(?i:department|dept\.?|office|institute|ministry|university|college|"
        r"board|council|directorate|school|faculty|centre|center|commission|"
        r"government|govt\.?|cell|committee|division) +(?i:of|for) +"
        r"(?:(?i:the) +)?[A-Z][\w&.'-]*(?: +(?:(?i:and|of|for|the|in) +|& +)?"
        r"[A-Z][\w&.'-]*){0,7}"
    ),
    re.compile(
        r"\b(?:[A-Z][a-z]+ +){1,4}(?:University|College|Academy|"
        r"Institute(?: +of +(?:Technology|Science|Management|Engineering))?)\b"
    ),
)
ORG_TRAILING = re.compile(r"(?: +(?:and|of|for|the|in|&))+$", IC)
ORG_LEADING = re.compile(
    r"(?:(?:The|This|That|Every|Each|Any|All|A|An|Our|Your|Such|Their|Its) +)+"
)


@dataclass
class Entity:
    entity_type: str
    page_number: int
    start: int
    end: int
    text: str
    normalized: str = ""
    value_date: datetime.date | None = None
    label: str = ""
    occurrences: int = 1
    position: int = 0
    extra: dict = field(default_factory=dict)


# Earlier wins when two matches overlap: an email address contains what looks
# like a URL, a URL a date, a reference number a course code.
PRIORITY = (
    "EMAIL",
    "URL",
    "MONEY",
    "DEADLINE",
    "DATE",
    "PERCENT",
    "DURATION",
    "PHONE",
    "REFERENCE",
    "COURSE_CODE",
    "ORG",
)


def _year(raw: str) -> int:
    value = int(raw)
    if len(raw) == 2:
        return 2000 + value if value < 70 else 1900 + value
    return value


def _month(raw: str) -> int:
    return MONTHS[raw[:3].lower()]


def _date(year, month, day):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _parse_date(match, pattern_index):
    groups = match.groupdict()
    year = _year(groups["year"])
    if not 1900 <= year <= 2100:
        return None, ""
    if pattern_index == 4:
        return None, f"{year:04d}-{_month(groups['month']):02d}"
    if pattern_index in (2, 3):
        value = _date(year, _month(groups["month"]), int(groups["a"]))
    elif pattern_index == 1:
        value = _date(year, int(groups["b"]), int(groups["a"]))
    else:
        first, second = int(groups["a"]), int(groups["b"])
        # Day first unless only the other reading is a valid date.
        value = _date(year, second, first) or _date(year, first, second)
    if value is None:
        return None, ""
    return value, value.isoformat()


def _context_before(text, start, limit=120):
    window_start = max(0, start - limit)
    window = text[window_start:start]
    # Only the current sentence or clause.
    cut = max(window.rfind(". "), window.rfind("\n\n"), window.rfind(";"))
    return window[cut + 1 :] if cut >= 0 else window


def _label(text, start, end):
    """What a date is for, in the document's own words."""
    sentence_start = max(
        text.rfind(". ", 0, start),
        text.rfind("\n\n", 0, start),
        text.rfind(";", 0, start),
    )
    sentence_end_candidates = [
        index for index in (text.find(". ", end), text.find("\n\n", end)) if index >= 0
    ]
    sentence_end = (
        min(sentence_end_candidates) if sentence_end_candidates else len(text)
    )
    sentence = text[sentence_start + 1 : sentence_end]
    match = DEADLINE_LABEL.search(sentence)
    if match:
        label = f"{match.group(1)} for {match.group('what')}"
    else:
        before = " ".join(text[sentence_start + 1 : start].split())
        before = re.sub(
            r"(?:\s*\b(?:on|or|by|before|from|dated?|w\.e\.f|is|are|of|the|"
            r"not\s+later\s+than|till|until|up\s*to)\b\.?\s*[:\-]?)+$",
            "",
            before,
            flags=IC,
        ).strip(" :-,")
        words = before.split()
        label = " ".join(words[-LABEL_WORDS:])
    label = " ".join(label.split()).strip(" :-,")
    if not label:
        return ""
    return (label[0].upper() + label[1:])[:200]


def _amount(raw, scale):
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return ""
    if scale:
        value *= SCALES[scale.lower().rstrip("s")]
    if value == value.to_integral_value():
        return str(int(value))
    return f"{value.quantize(Decimal('0.01'))}"


def _digits(text):
    return re.sub(r"\D", "", text)


def _phone_normalized(raw):
    digits = _digits(raw)
    stripped = raw.strip()
    if stripped.startswith("+"):
        return f"+{digits}"
    if stripped.startswith("00"):
        return f"+{digits[2:]}"
    if digits.startswith("1800"):
        return digits
    if len(digits) == 11 and digits[0] == "0" and digits[1] in "6789":
        return f"+91{digits[1:]}"
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    if digits.startswith("0") and 10 <= len(digits) <= 12:
        return f"+91{digits[1:]}"
    return digits


def _find_on_page(page_number, text):
    found = []

    def add(entity_type, start, end, normalized="", **kwargs):
        found.append(
            Entity(
                entity_type=entity_type,
                page_number=page_number,
                start=start,
                end=end,
                text=text[start:end],
                normalized=normalized,
                **kwargs,
            )
        )

    for match in EMAIL.finditer(text):
        add("EMAIL", match.start(), match.end(), match.group().lower())

    for match in URL.finditer(text):
        raw = match.group().rstrip(".,;:!?")
        end = match.start() + len(raw)
        normalized = raw if "://" in raw else f"http://{raw}"
        scheme, _, rest = normalized.partition("://")
        host, slash, path = rest.partition("/")
        add(
            "URL", match.start(), end, f"{scheme.lower()}://{host.lower()}{slash}{path}"
        )

    for pattern, currency in MONEY_PATTERNS:
        for match in pattern.finditer(text):
            amount = _amount(match.group("amount"), match.groupdict().get("scale"))
            if amount:
                add("MONEY", match.start(), match.end(), f"{currency} {amount}")

    for index, pattern in enumerate(DATE_PATTERNS):
        for match in pattern.finditer(text):
            value, normalized = _parse_date(match, index)
            if not normalized and index != 4:
                continue
            context = _context_before(text, match.start())
            after = text[match.end() : match.end() + 40]
            deadline = index != 4 and (
                DEADLINE_BEFORE.search(context) or DEADLINE_AFTER.match(after)
            )
            add(
                "DEADLINE" if deadline else "DATE",
                match.start(),
                match.end(),
                normalized,
                value_date=value,
                label=_label(text, match.start(), match.end()),
            )

    for match in PERCENT.finditer(text):
        value = match.group("value")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        add("PERCENT", match.start(), match.end(), f"{value}%")

    for match in DURATION.finditer(text):
        count = match.group("count").lower()
        number = NUMBER_WORDS.get(count) or int(count)
        unit = match.group("unit").lower().split()[-1].rstrip("s")
        code, is_time = DURATION_CODES.get(unit, ("", False))
        normalized = f"PT{number}{code}" if is_time else f"P{number}{code}"
        add("DURATION", match.start(), match.end(), normalized if code else "")

    for pattern, needs_cue in PHONE_PATTERNS:
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 60) : match.start()]
            if PHONE_REFUSE_BEFORE.search(before[-25:]):
                continue
            if needs_cue and not PHONE_CUE.search(before):
                continue
            digits = _digits(match.group())
            if not 10 <= len(digits) <= 14:
                continue
            add("PHONE", match.start(), match.end(), _phone_normalized(match.group()))

    for match in REFERENCE_LABELLED.finditer(text):
        value = match.group("value").rstrip(".-/)(")
        if not any(ch.isdigit() for ch in value):
            continue
        start = match.start("value")
        add("REFERENCE", start, start + len(value), value.upper())
    for match in REFERENCE_SLASHED.finditer(text):
        value = match.group()
        if "/" not in value or not any(ch.isdigit() for ch in value):
            continue
        add("REFERENCE", match.start(), match.end(), value.upper())
    for match in REGULATION_CODE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start : line_end if line_end >= 0 else len(text)]
        if REGULATION_WORD.search(line):
            add("REFERENCE", match.start(), match.end(), match.group("value"))

    for match in COURSE_LABELLED.finditer(text):
        start = match.start("value")
        value = match.group("value")
        add("COURSE_CODE", start, start + len(value), value.replace(" ", "").upper())
    for match in COURSE_CODE.finditer(text):
        letters, digits = match.group("letters"), match.group("digits")
        if letters in COURSE_CODE_REFUSED:
            continue
        if len(digits) == 4 and digits[:2] in ("19", "20"):
            continue
        add(
            "COURSE_CODE",
            match.start(),
            match.end(),
            f"{letters}{digits}{match.group('suffix')}",
        )

    for pattern in ORG_PATTERNS:
        for match in pattern.finditer(text):
            start = match.start()
            raw = ORG_TRAILING.sub("", match.group()).rstrip(".,;:-")
            # "The University", "Every College": a determiner, not a name.
            leading = ORG_LEADING.match(raw)
            if leading:
                start += leading.end()
                raw = raw[leading.end() :]
            if len(raw.split()) < 2:
                continue
            add("ORG", start, start + len(raw), " ".join(raw.split()))

    return found


def _resolve_overlaps(entities):
    """Keep the higher-priority entity wherever two overlap."""
    rank = {name: index for index, name in enumerate(PRIORITY)}
    ordered = sorted(
        entities, key=lambda e: (rank[e.entity_type], -(e.end - e.start), e.start)
    )
    kept = []
    taken = {}
    for entity in ordered:
        spans = taken.setdefault(entity.page_number, [])
        if any(entity.start < end and start < entity.end for start, end in spans):
            continue
        spans.append((entity.start, entity.end))
        kept.append(entity)
    return kept


def extract_entities(pages) -> list:
    """Every entity in the document, deduplicated, first occurrence kept."""
    found = []
    for page_number, text in pages:
        found.extend(_resolve_overlaps(_find_on_page(page_number, text or "")))

    found.sort(key=lambda e: (e.page_number, e.start))
    merged = {}
    for entity in found:
        key = (
            entity.entity_type,
            (entity.normalized or " ".join(entity.text.split())).casefold(),
        )
        if key in merged:
            merged[key].occurrences += 1
            continue
        merged[key] = entity

    # A date that is a deadline somewhere is a deadline: one row, the
    # deadline's span and label, the date's occurrences folded in.
    for (entity_type, value), entity in list(merged.items()):
        if entity_type == "DATE" and ("DEADLINE", value) in merged:
            merged[("DEADLINE", value)].occurrences += entity.occurrences
            del merged[(entity_type, value)]

    entities = sorted(merged.values(), key=lambda e: (e.page_number, e.start))
    if len(entities) > MAX_ENTITIES:
        keep = sorted(entities, key=lambda e: -e.occurrences)[:MAX_ENTITIES]
        entities = sorted(keep, key=lambda e: (e.page_number, e.start))
    for position, entity in enumerate(entities):
        entity.position = position
    return entities

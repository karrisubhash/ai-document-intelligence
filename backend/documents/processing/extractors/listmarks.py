"""The numbers and letters a list is counted in.

Word and PowerPoint both store a numbered list as a recipe ("lower roman,
followed by a period"), not as text, so the marker has to be generated. The
two formats name the same few styles differently; this module is the one place
that knows how each is written out.
"""

_ROMAN = (
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
)


def roman(number: int, upper: bool) -> str:
    out = ""
    for value, symbol in _ROMAN:
        while number >= value:
            out += symbol
            number -= value
    return out.upper() if upper else out


def letters(number: int, upper: bool) -> str:
    """1 -> a, 26 -> z, 27 -> aa: the way a long lettered list runs on."""
    out = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        out = chr(ord("a") + remainder) + out
    return out.upper() if upper else out


def format_number(number: int, fmt: str) -> str:
    """A count in a Word numbering format (`w:numFmt`)."""
    if fmt == "lowerLetter":
        return letters(number, False)
    if fmt == "upperLetter":
        return letters(number, True)
    if fmt == "lowerRoman":
        return roman(number, False)
    if fmt == "upperRoman":
        return roman(number, True)
    if fmt == "decimalZero":
        return f"{number:02d}"
    if fmt == "none":
        return ""
    return str(number)


# PowerPoint's `a:buAutoNum type`: a base style plus how it is closed.
_PPT_BASE = {
    "arabic": "decimal",
    "alphaLc": "lowerLetter",
    "alphaUc": "upperLetter",
    "romanLc": "lowerRoman",
    "romanUc": "upperRoman",
}


def format_auto_number(number: int, kind: str) -> str:
    """A count in a PowerPoint auto-number type, e.g. "arabicPeriod" -> "3."."""
    for prefix, base in _PPT_BASE.items():
        if kind.startswith(prefix):
            suffix = kind[len(prefix) :]
            value = format_number(number, base)
            if suffix == "ParenBoth":
                return f"({value})"
            if suffix == "ParenR":
                return f"{value})"
            if suffix == "Period":
                return f"{value}."
            return value
    return f"{number}."

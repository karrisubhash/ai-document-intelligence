"""Text normalization applied before anything is stored or measured.

One definition, imported by both the pipeline that writes page text and the
layout analysis that records offsets into it. Two copies of this rule would
be a silent corruption rather than a bug: if a stage detected a table in the
unstripped text and the pipeline stored the stripped version, every span
after the first NUL byte would point a few characters to the left, and the
verification that is supposed to catch fabricated cells would reject real
ones instead.
"""

import re

# PostgreSQL text columns cannot hold a NUL byte, and extraction produces
# them - a malformed PDF text layer and a bad OCR pass both do it.
# Stripping here rather than letting the write fail: the alternative is a
# document that processes perfectly and then dies at the final INSERT with a
# database error that says nothing about which page was wrong.
NUL = "\x00"


def strip_nul(text: str) -> str:
    return text.replace(NUL, "") if NUL in text else text


# A table written as text has its cells separated by a pipe: "Form No | F-12".
# The patterns that read labels and values - field rules, deadline and phone
# cues - allow whitespace, a colon or a hyphen between a label and its value,
# so a pipe in the way would hide a label beside its value in a table the
# extractor read perfectly well. Only a pipe that stands apart (whitespace or
# the edge of the text on both sides) is a cell separator; one inside a word
# is left alone.
_CELL_PIPE = re.compile(r"(?<![^\s])\|(?![^\s])")


def matchable(text: str) -> str:
    """The text label/value patterns are run on: cell separators as spaces.

    A SAME-LENGTH view, character for character, so every offset found in it
    is an offset in the stored text. What a match SAYS is always taken from
    the original, never from this view.
    """
    return _CELL_PIPE.sub(" ", text)

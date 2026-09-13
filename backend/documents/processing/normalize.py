"""Text normalization applied before anything is stored or measured.

One definition, imported by both the pipeline that writes page text and the
layout analysis that records offsets into it. Two copies of this rule would
be a silent corruption rather than a bug: if a stage detected a table in the
unstripped text and the pipeline stored the stripped version, every span
after the first NUL byte would point a few characters to the left, and the
verification that is supposed to catch fabricated cells would reject real
ones instead.
"""

# PostgreSQL text columns cannot hold a NUL byte, and extraction produces
# them - a malformed PDF text layer and a bad OCR pass both do it.
# Stripping here rather than letting the write fail: the alternative is a
# document that processes perfectly and then dies at the final INSERT with a
# database error that says nothing about which page was wrong.
NUL = "\x00"


def strip_nul(text: str) -> str:
    return text.replace(NUL, "") if NUL in text else text

"""The markers around a figure reading in page text, and finding them again.

vision.py writes a reading between two marker lines. Everything after that
- the chunker keeping the reading whole, the prompt telling the answering
model that a passage holds a machine-written description - has to find it
again in the stored text, so the pattern lives in one place, with no Django
and no model code, and the chunker can import it without pulling in the rest.
"""

import re

BEGIN_PREFIX = "[Figure: "
END_LINE = "[End of figure]"

# From a BEGIN line to the next END line, or to the end of the text when the
# END line was lost (a chunk cut the reading short). Non-greedy, so two
# figures on a page are two spans.
_SPAN = re.compile(
    r"^\[Figure: [^\n\]]*\][ \t]*\n.*?(?:^\[End of figure\][ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL,
)


def spans(text: str) -> list:
    """[(start, end)] of each figure reading in `text`, in order."""
    if BEGIN_PREFIX not in text:
        return []
    return [(match.start(), match.end()) for match in _SPAN.finditer(text)]


def title(reading: str) -> str:
    """The line after the opening marker: what the figure is called."""
    lines = reading.split("\n")
    return lines[1].strip() if len(lines) > 1 and lines[1].strip() != END_LINE else ""


def contains(text: str) -> bool:
    return BEGIN_PREFIX in text

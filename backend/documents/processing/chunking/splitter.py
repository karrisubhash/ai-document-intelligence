"""Chunking: turning page text into retrievable units.

Pure functions over `(page_number, text)` pairs, with no Django import, for
the same reason the field matcher is - the strategy can then be benchmarked
offline against an evaluation set without a database.

Two rules shape the whole design.

*Chunks never span pages.* A chunk that straddles a page boundary cannot
answer "which page did this come from", and page number is half of the
citation model in section 12.3. Keeping chunks inside a page costs a little
packing efficiency and buys every retrieved passage a real location.

*Offsets are into the stored page text.* `page_text[start:end]` must return
the chunk verbatim. That is the same invariant field extraction enforces,
and it is what lets a retrieved chunk be highlighted in the source later
rather than merely quoted.
"""

import re
from dataclasses import dataclass, field

# Roughly 1000 characters is a few hundred tokens - large enough to carry a
# self-contained clause or paragraph, small enough that a retrieved chunk is
# mostly relevant rather than mostly padding.
DEFAULT_CHUNK_SIZE = 1000

# Overlap so a sentence cut by a boundary still appears whole in one of the
# two neighbours. Without it, the passage that answers a question is exactly
# the one most likely to be split down the middle.
DEFAULT_OVERLAP = 150

# Below this, a trailing fragment is merged backwards instead of being
# emitted as a chunk of its own - a 20-character chunk retrieves noise.
MIN_CHUNK_SIZE = 100

# Preferred split points, strongest first. Splitting on structure beats
# splitting at a fixed offset, because it keeps clauses intact.
BOUNDARY_PATTERNS = (
    re.compile(r"\n\s*\n"),  # paragraph break
    re.compile(r"(?<=[.!?])\s+"),  # sentence end
    re.compile(r"\n"),  # line break
    re.compile(r"\s+"),  # any whitespace, last resort
)


@dataclass(frozen=True)
class Chunk:
    page_number: int
    chunk_index: int
    text: str
    character_start: int
    character_end: int
    metadata: dict = field(default_factory=dict)

    @property
    def length(self) -> int:
        return self.character_end - self.character_start


def _best_split(text: str, start: int, end: int) -> int:
    """The latest structural boundary inside [start, end), or `end`.

    Searches the tail of the window so chunks stay near the target size
    rather than collapsing to the first paragraph break they meet.
    """
    window = text[start:end]
    # Only look for a boundary in the last third; a break near the start
    # would produce a chunk far smaller than asked for.
    floor = max(0, int(len(window) * 0.6))

    for pattern in BOUNDARY_PATTERNS:
        best = None
        for match in pattern.finditer(window, floor):
            best = match.end()
        if best is not None and best > 0:
            return start + best

    return end


def _split_prose(text: str, region_start: int, region_end: int, chunk_size, overlap):
    """Windowed split of `text[region_start:region_end]`, as absolute spans."""
    spans = []
    position = region_start

    while position < region_end:
        end = min(position + chunk_size, region_end)

        if end < region_end:
            end = _best_split(text, position, end)

        # Trim to the stored text's own boundaries so the offsets still
        # re-slice exactly, then record the trimmed span - not the raw one.
        raw = text[position:end]
        stripped = raw.strip()

        if stripped:
            leading = len(raw) - len(raw.lstrip())
            absolute_start = position + leading
            absolute_end = absolute_start + len(stripped)

            remaining = region_end - end
            if remaining and remaining < MIN_CHUNK_SIZE:
                # Absorb a short tail rather than emit a fragment.
                absolute_end = len(text[:region_end].rstrip())
                end = region_end

            spans.append((absolute_start, absolute_end))

        if end >= region_end:
            break

        # Step forward, leaving `overlap` characters of context behind.
        step = max(1, (end - position) - overlap)
        position += step

    return spans


def _split_table(text: str, region_start: int, region_end: int, chunk_size: int):
    """Split a table, at row boundaries only, as absolute spans.

    No overlap. Overlapping rows between two chunks would put the same
    figure in retrieval twice and let one row be quoted from two places
    with different neighbours - the opposite of what a citation is for.
    """
    spans = []
    position = region_start

    while position < region_end:
        limit = min(position + chunk_size, region_end)

        if limit < region_end:
            # Back up to a row boundary. A table cut mid-row produces a
            # half-row that reads like a complete one.
            cut = text.rfind("\n", position, limit)
            if cut > position:
                limit = cut + 1

        raw = text[position:limit]
        stripped = raw.strip()
        if stripped:
            leading = len(raw) - len(raw.lstrip())
            absolute_start = position + leading
            spans.append((absolute_start, absolute_start + len(stripped)))

        if limit <= position:
            break
        position = limit

    return spans


def _regions(text: str, tables) -> list:
    """The page as alternating prose and table regions, in order.

    A table whose span is invalid, or overlaps one already taken, is
    dropped rather than trusted - the chunker is downstream of detection
    and is not the place to adjudicate a bad span.
    """
    regions = []
    cursor = 0

    for table in sorted(tables, key=lambda item: item.character_start):
        start = max(0, min(table.character_start, len(text)))
        end = max(0, min(table.character_end, len(text)))
        if end <= start or start < cursor:
            continue

        if start > cursor:
            regions.append((cursor, start, None))
        regions.append((start, end, table))
        cursor = end

    if cursor < len(text):
        regions.append((cursor, len(text), None))

    return regions


def split_page(
    page_number: int,
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    start_index: int = 0,
    tables=(),
) -> list:
    """Split one page's text into chunks.

    Returns chunks whose offsets index into `text` exactly. A page that is
    blank or whitespace-only yields nothing - an empty chunk is not a
    retrievable unit, and indexing one would pollute every search.

    `tables` are spans that must not be cut arbitrarily. A table split at
    an offset loses the rows either side of the cut from each other, and -
    worse - strands the data rows from the header, which is the only thing
    that makes "85000" mean a tuition fee rather than a number.

    The header is NOT prepended to the chunk text. It could not be: a
    chunk's text must equal `page_text[start:end]`, and inventing a
    prefix would break the one invariant that makes a citation checkable.
    It travels in metadata instead, where the context builder can put it
    in front of the rows without it ever being mistaken for page content.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")

    chunks = []
    if not text.strip():
        return chunks

    index = start_index

    for region_start, region_end, table in _regions(text, tables):
        if table is None:
            spans = _split_prose(text, region_start, region_end, chunk_size, overlap)
            metadata_for = lambda part, parts: {}  # noqa: E731
        else:
            spans = _split_table(text, region_start, region_end, chunk_size)

            def metadata_for(part, parts, table=table):
                metadata = {"kind": "TABLE", "table_index": table.table_index}
                header = list(getattr(table, "header", []) or [])
                if header:
                    metadata["header"] = header
                if parts > 1:
                    # A reader - and the answerer - should know this is
                    # part of a larger table rather than all of it.
                    metadata["part"] = part
                    metadata["parts"] = parts
                return metadata

        for part, (start, end) in enumerate(spans, start=1):
            chunks.append(
                Chunk(
                    page_number=page_number,
                    chunk_index=index,
                    text=text[start:end],
                    character_start=start,
                    character_end=end,
                    metadata=metadata_for(part, len(spans)),
                )
            )
            index += 1

    return chunks


def split_pages(pages, *, tables_by_page=None, **kwargs) -> list:
    """Chunk an entire document, numbering chunks across pages.

    `pages` is an iterable of (page_number, text), the same shape the
    field extractor consumes. `tables_by_page` maps a page number to the
    tables found on it, which the LAYOUT_ANALYSIS stage has already
    verified against that page's text.
    """
    tables_by_page = tables_by_page or {}
    chunks = []

    for page_number, text in pages:
        page_chunks = split_page(
            page_number,
            text,
            start_index=len(chunks),
            tables=tables_by_page.get(page_number, ()),
            **kwargs,
        )
        chunks.extend(page_chunks)

    return chunks


def verify(chunks, pages) -> None:
    """Assert every chunk's offsets re-slice out of its page.

    Called by the service before anything is written. A chunk whose offsets
    do not reproduce its text is not a citation, and storing one would make
    highlighting a source impossible to trust later.
    """
    by_page = dict(pages)

    for chunk in chunks:
        text = by_page.get(chunk.page_number)
        if text is None:
            raise ValueError(
                f"chunk {chunk.chunk_index} cites page {chunk.page_number}, "
                f"which the document does not have"
            )
        actual = text[chunk.character_start : chunk.character_end]
        if actual != chunk.text:
            raise ValueError(
                f"chunk {chunk.chunk_index} span "
                f"[{chunk.character_start}:{chunk.character_end}] on page "
                f"{chunk.page_number} holds {actual[:40]!r}, but the chunk "
                f"claims {chunk.text[:40]!r}"
            )

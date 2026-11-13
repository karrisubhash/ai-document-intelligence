"""What the Search page shows for each hit: the document it belongs to, the
words that matched, a window of text around them, and hits grouped by
document.

Nothing here retrieves, reorders or filters - the hits arrive authorized and
ranked (service.search, ranking.search), and leave in the same order. What
this adds is presentation the client could not get right on its own:

*Highlights that agree with the search.* The page used to highlight with a
case-insensitive substring match of the query's words, so "act" lit up
inside "contract" while the stemmed matches the search had actually found
("regulations" against "regulation") stayed plain. Here PostgreSQL marks the
words with ts_headline, using the same text-search configuration and the
same tsquery as the lexical retriever, and the markers become code-point
offsets into the hit's text - spans, never HTML, so nothing from a document
is ever rendered as markup.

*Snippets centred on the match.* The page showed the first 420 characters of
a chunk of up to 1,000, so the words that matched were often cut off.
`snippet` is a window around the first highlight instead.

*One card per document.* `group_by: "document"` returns documents in the
order of their best passage, each with its best few passages, and merges
passages of the same page that overlap or touch - chunks overlap by
CHUNK_OVERLAP characters, so two hits from neighbouring chunks repeated the
same sentences.

The document's metadata comes from the rows the candidates were already
fetched with (`service._candidates` select_related the version and the
document), so it costs no query; the highlights cost one, over the hits on
the page only.
"""

import os

from django.contrib.postgres.search import SearchHeadline

from documents.models import DocumentChunk

from . import service

# Markers ts_headline puts around each matched word. Control characters that
# extracted text does not contain; a chunk that did contain one is detected
# (the text without markers would differ) and simply gets no highlights.
START = "\x01"
STOP = "\x02"

# Characters of context in a snippet, and how much of it goes before the
# first matched word.
SNIPPET_CHARS = 320
SNIPPET_LEAD = 90

# Passages per document in a group; the rest are counted, not sent.
PASSAGES_PER_DOCUMENT = 3

FILE_KINDS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "text",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}


def highlight_spans(hits, query: str) -> dict:
    """{chunk id: [(start, end), ...]} - code points into each chunk's text.

    One query for every hit. A chunk ts_headline cannot reproduce exactly
    gets no spans rather than misplaced ones.
    """
    if not hits or not query.strip():
        return {}
    texts = {hit.chunk.id: hit.chunk.text for hit in hits}
    rows = (
        DocumentChunk.objects.filter(id__in=list(texts))
        .annotate(
            marked=SearchHeadline(
                "text",
                service._build_lexical_query(query),
                config="english",
                start_sel=START,
                stop_sel=STOP,
                highlight_all=True,
            )
        )
        .values_list("id", "marked")
    )
    return {
        chunk_id: spans_from_markers(marked, texts[chunk_id])
        for chunk_id, marked in rows
    }


def spans_from_markers(marked: str, original: str) -> list:
    """The marked words of `marked` as offsets into `original`.

    Words separated only by spaces are joined into one span ("minimum
    attendance"), which reads as one phrase rather than two boxes.
    """
    spans = []
    plain = []
    start = None
    for character in marked or "":
        if character == START:
            start = len(plain)
        elif character == STOP:
            if start is not None and len(plain) > start:
                spans.append([start, len(plain)])
            start = None
        else:
            plain.append(character)
    if "".join(plain) != original:
        return []
    joined = []
    for span in spans:
        if joined and original[joined[-1][1] : span[0]].strip(" ") == "":
            joined[-1][1] = span[1]
        else:
            joined.append(span)
    return [tuple(span) for span in joined]


def snippet_window(text: str, spans) -> tuple[int, int]:
    """(start, end) of about SNIPPET_CHARS of `text`, around the first span.

    Cut at whitespace so a word is never halved; the whole text when it is
    short enough.
    """
    length = len(text)
    if length <= SNIPPET_CHARS:
        return 0, length
    anchor = spans[0][0] if spans else 0
    start = max(0, min(anchor - SNIPPET_LEAD, length - SNIPPET_CHARS))
    if start:
        space = text.find(" ", start)
        if 0 <= space < anchor:
            start = space + 1
    end = min(length, start + SNIPPET_CHARS)
    if end < length:
        space = text.rfind(" ", start, end)
        if space > (spans[0][1] if spans else start):
            end = space
    return start, end


def file_kind(file_name: str) -> str:
    return FILE_KINDS.get(os.path.splitext(file_name or "")[1].lower(), "other")


def document_fields(chunk) -> dict:
    """The document a chunk belongs to, from rows already fetched."""
    version = chunk.document_version
    document = version.document
    return {
        "document_type": document.document_type,
        "department": document.department,
        "document_updated_at": document.updated_at,
        "file_name": version.file_name,
        "file_kind": file_kind(version.file_name),
        "version_number": version.version_number,
    }


def hit_payload(hit, spans) -> dict:
    """One result as the endpoint returns it (the flat `results` list)."""
    chunk = hit.chunk
    start, end = snippet_window(chunk.text, spans)
    return {
        **hit.citation,
        **document_fields(chunk),
        # "TABLE" for a chunk cut from a detected table; "" for prose.
        "chunk_kind": (chunk.metadata or {}).get("kind", ""),
        # Retrieval's fused score; `relevance` is the reranker's, and is what
        # the results are ordered by.
        "score": hit.score,
        "relevance": hit.relevance,
        "below_floor": hit.below_floor,
        "matched_by": list(hit.matched_by),
        "lexical_rank": hit.lexical_rank,
        "vector_rank": hit.vector_rank,
        "highlights": [{"start": s, "end": e} for s, e in spans],
        "snippet": {"start": start, "end": end},
    }


def group_by_document(results: list) -> list:
    """Results (hit payloads, ranked) as one entry per document.

    Documents come in the order of their best passage. Within a document,
    passages of the same page that overlap or touch are merged - when their
    texts agree on the overlap, which they do for chunks cut from one page
    (a table chunk, whose text is rebuilt from cells, is never merged).
    """
    groups = {}
    for result in results:
        group = groups.get(result["document_id"])
        if group is None:
            group = groups[result["document_id"]] = {
                "document_id": result["document_id"],
                "document_title": result["document_title"],
                "document_type": result["document_type"],
                "department": result["department"],
                "updated_at": result["document_updated_at"],
                "file_name": result["file_name"],
                "file_kind": result["file_kind"],
                "version_number": result["version_number"],
                "relevance": result["relevance"],
                "matched_by": [],
                "passages": [],
            }
        for way in result["matched_by"]:
            if way not in group["matched_by"]:
                group["matched_by"].append(way)
        passage = _passage(result)
        for existing in group["passages"]:
            if _merge(existing, passage):
                break
        else:
            group["passages"].append(passage)

    out = []
    for group in groups.values():
        passages = group.pop("passages")
        group["passages"] = passages[:PASSAGES_PER_DOCUMENT]
        group["more_passages"] = max(0, len(passages) - PASSAGES_PER_DOCUMENT)
        out.append(group)
    return out


def _passage(result) -> dict:
    return {
        "chunk_ids": [result["chunk_id"]],
        "page_number": result["page_number"],
        "character_start": result["character_start"],
        "character_end": result["character_end"],
        "text": result["text"],
        "highlights": [dict(span) for span in result["highlights"]],
        "snippet": dict(result["snippet"]),
        "relevance": result["relevance"],
        "matched_by": list(result["matched_by"]),
        "chunk_kind": result.get("chunk_kind", ""),
    }


def _merge(kept: dict, other: dict) -> bool:
    """Fold `other` into `kept` when they are one stretch of one page."""
    if kept["page_number"] != other["page_number"]:
        return False
    if "TABLE" in (kept["chunk_kind"], other["chunk_kind"]):
        return False
    first, second = sorted((kept, other), key=lambda p: p["character_start"])
    if second["character_start"] > first["character_end"]:
        return False
    overlap = first["character_end"] - second["character_start"]
    if overlap > len(second["text"]):
        # `second` lies inside `first`: same text, nothing to add.
        inner = second["character_start"] - first["character_start"]
        if first["text"][inner : inner + len(second["text"])] != second["text"]:
            return False
        text, shift = first["text"], None
    else:
        if overlap and first["text"][-overlap:] != second["text"][:overlap]:
            return False
        text = first["text"] + second["text"][overlap:]
        shift = second["character_start"] - first["character_start"]

    spans = [(span["start"], span["end"]) for span in first["highlights"]]
    if shift is not None:
        spans += [
            (span["start"] + shift, span["end"] + shift)
            for span in second["highlights"]
        ]
    merged_spans = []
    for start, end in sorted(set(spans)):
        if merged_spans and start <= merged_spans[-1][1]:
            merged_spans[-1][1] = max(merged_spans[-1][1], end)
        else:
            merged_spans.append([start, end])

    start, end = snippet_window(text, merged_spans)
    relevances = [p["relevance"] for p in (kept, other) if p["relevance"] is not None]
    kept.update(
        chunk_ids=kept["chunk_ids"] + other["chunk_ids"],
        character_start=first["character_start"],
        character_end=max(first["character_end"], second["character_end"]),
        text=text,
        highlights=[{"start": s, "end": e} for s, e in merged_spans],
        snippet={"start": start, "end": end},
        relevance=max(relevances) if relevances else None,
        matched_by=list(dict.fromkeys(kept["matched_by"] + other["matched_by"])),
    )
    return True

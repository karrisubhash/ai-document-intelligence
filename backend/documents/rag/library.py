"""Questions about the library itself, answered from the catalogue.

Five of about forty questions asked on the live site (2026-10-24 to 11-18)
were about the documents as a whole, not about anything in them:

    "What are the documents uploaded"
    "Uploaded document related to?"
    "What Tells the uploaded document"
    "Recently uploaded document summary"

Retrieval cannot answer these. No passage says what the library holds or which
file arrived last, so each one was refused ("I couldn't find enough
information"), answered from general knowledge ("I cannot see your files"), or
-- worst -- a model summarised whichever passages happened to rank first and
the summary was thrown away by the grounding check.

The catalogue CAN answer them, exactly: the documents this user may read
(authorization.visible_documents, the same rule as the Documents page and as
retrieval), their titles, page counts and upload dates, and each one's brief
(DocumentInsight: an extractive summary whose every sentence is a span of the
document). Nothing here calls a model, so it costs none of a free tier's
requests, and every sentence it states is cited to the page it came from.

WHAT COUNTS AS A LIBRARY QUESTION. Only a question that names documents
("document", "file", "pdf", "upload") and has NO other content word. "What
documents are required for admission?" has "required" and "admission", and is
a question about the documents' CONTENT, answered by retrieval as before. One
exception: a request for a summary whose remaining words all come from one
document's title ("summarise the GNSS lecture") is that document's summary.
"""

import re
from dataclasses import dataclass

from documents import authorization
from documents.models import (
    Collection,
    Document,
    DocumentChunk,
    DocumentInsight,
    ProcessingJob,
)
from documents.processing.tables import text_rows

from .labels import LIBRARY_MODEL_NAME

# The answerer name stored on a library answer (Message.model_name), so it is
# labelled as coming from the catalogue rather than quoted or written by a model.
MODEL_NAME = LIBRARY_MODEL_NAME
MODEL_VERSION = "1"

# A long library is listed up to this many documents; the rest are counted.
LIST_LIMIT = 15
SUMMARY_SENTENCES = 4
KEY_POINTS = 4
DESCRIPTION_CHARACTERS = 180

_ANCHORS = frozenset(
    {
        "document",
        "documents",
        "doc",
        "docs",
        "file",
        "files",
        "pdf",
        "pdfs",
        "upload",
        "uploads",
        "uploaded",
        "library",
    }
)
_PLURAL = frozenset(
    {"documents", "docs", "files", "pdfs", "uploads", "all", "list", "which", "many"}
)
_RECENT = frozenset({"recent", "recently", "latest", "last", "newest", "new", "just"})
_SUMMARY = frozenset(
    {
        "summary",
        "summarise",
        "summarize",
        "summarised",
        "summarized",
        "overview",
        "about",
        "describe",
        "explain",
        "brief",
        "briefly",
        "tell",
        "tells",
        "say",
        "says",
        "contain",
        "contains",
        "content",
        "contents",
        "cover",
        "covers",
        "related",
        "relate",
    }
)
# Everything else a library question is made of. A word outside all of these
# sets is a content word, and makes the question one about the documents'
# content instead.
_FRAME = frozenset(
    {
        "show",
        "give",
        "topic",
        "topics",
        "subject",
        "subjects",
        "kind",
        "kinds",
        "type",
        "types",
        "number",
        "count",
        "available",
        "system",
        "platform",
        "here",
        "have",
        "has",
        "had",
        "my",
        "mine",
        "our",
        "each",
        "every",
        "these",
        "those",
        "this",
        "that",
        "there",
        "what",
        "whats",
        "is",
        "are",
        "was",
        "were",
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "to",
        "me",
        "i",
        "we",
        "you",
        "do",
        "does",
        "did",
        "it",
        "its",
        "they",
        "them",
        "for",
        "with",
        "please",
        "pls",
        "plz",
        "short",
        "name",
        "names",
        "title",
        "titles",
        "one",
        "ones",
        "so",
        "far",
        "till",
        "now",
        "currently",
        "current",
        "added",
        "stored",
        "saved",
        "and",
        "or",
        "from",
        "by",
        "how",
        "main",
        "key",
        "point",
        "points",
        "provide",
        "can",
        "could",
        "would",
        "be",
        "all",
        "list",
        "which",
        "many",
        "uploading",
        "s",
    }
)
_KNOWN = _ANCHORS | _PLURAL | _RECENT | _SUMMARY | _FRAME
# "How do I upload a document?", "Can I upload a PDF?": questions about using
# the platform, not about what it holds. "How many documents" still counts.
_NOT_ABOUT_HOLDINGS = frozenset(
    {"how", "why", "can", "could", "should", "would", "where", "when", "will"}
)
_WORD = re.compile(r"[a-z]+")


@dataclass
class Intent:
    """What a library question asks for."""

    kind: str  # "list" or "summary"
    recent: bool = False
    document: Document | None = None


def _words(text: str) -> list:
    return _WORD.findall((text or "").lower())


def classify(question: str):
    """("list" | "summary", leftover words) for a library question, else None.

    Pure: it reads the question only. A summary request with leftover words is
    returned with them, for `intent` to match against document titles.
    """
    words = _words(question)
    if not words or len(words) > 20 or not _ANCHORS & set(words):
        return None
    if _NOT_ABOUT_HOLDINGS & set(words) and "many" not in words:
        return None
    leftover = [word for word in words if word not in _KNOWN]
    wants_summary = bool(_SUMMARY & set(words))
    if leftover:
        # Only an explicit request for a summary: "the last DATE to upload
        # documents" has a recency word and is a question about a deadline.
        return ("summary", leftover) if wants_summary else None
    if _RECENT & set(words):
        return ("summary", [])
    if _PLURAL & set(words):
        return ("list", [])
    # "the uploaded document": a summary if there is one document, else a list.
    return ("summary" if wants_summary else "list", [])


def _scope(
    user, *, document_id=None, collection_id=None, document_type="", department=""
):
    documents = authorization.visible_documents(
        user, Document.objects.filter(archived_at__isnull=True)
    )
    if document_id:
        documents = documents.filter(id=document_id)
    if collection_id:
        visible = authorization.visible_collections(
            user, Collection.objects.filter(pk=collection_id)
        ).exists()
        documents = (
            documents.filter(collections=collection_id) if visible else documents.none()
        )
    if document_type:
        documents = documents.filter(document_type=document_type)
    if department:
        documents = documents.filter(department=department)
    return documents.select_related("current_version__current_content").order_by(
        "-created_at", "-id"
    )


def intent(user, question: str, **scope) -> Intent | None:
    """The library question `question` asks, or None when it is not one."""
    found = classify(question)
    if found is None:
        return None
    kind, leftover = found
    documents = _scope(user, **scope)
    ready = [doc for doc in documents if doc.status == Document.Status.COMPLETED]

    if scope.get("document_id"):
        # Chat about one document: every library question is about that one.
        return Intent("summary", document=ready[0]) if ready else None

    if leftover:
        # "Summarise the GNSS lecture": the leftover words must all be in a
        # document's title, or this is a question for retrieval. The newest
        # such document when several share the words (the same file uploaded
        # twice).
        wanted = set(leftover)
        named = [doc for doc in ready if wanted <= set(_words(doc.title))]
        return Intent("summary", document=named[0]) if named else None

    if kind == "summary":
        recent = bool(_RECENT & set(_words(question)))
        if recent and ready:
            return Intent("summary", recent=True, document=ready[0])
        if len(ready) == 1:
            return Intent("summary", document=ready[0])
    return Intent("list")


def _pages(document) -> int:
    content = getattr(document.current_version, "current_content", None)
    return getattr(content, "page_count", 0) or 0


def _uploaded(document) -> str:
    return f"{document.created_at.day} {document.created_at:%b %Y}"


def _insight(document):
    content = getattr(document.current_version, "current_content", None)
    if content is None:
        return None
    return DocumentInsight.objects.filter(
        document_content=content, status=DocumentInsight.Status.READY
    ).first()


def _chunks(document):
    version = document.current_version
    if version is None:
        return DocumentChunk.objects.none()
    return DocumentChunk.objects.filter(
        document_version=version, document_content=version.current_content
    ).order_by("page_number", "character_start", "chunk_index")


def _chunk_for(document, page_number=None, start=None):
    """The chunk holding a brief sentence (page and offset), or the first one."""
    chunks = _chunks(document)
    if page_number is not None:
        on_page = chunks.filter(page_number=page_number)
        if start is not None:
            holding = on_page.filter(
                character_start__lte=start, character_end__gt=start
            ).first()
            if holding is not None:
                return holding
        first = on_page.first()
        if first is not None:
            return first
    return chunks.first()


def _citation(marker: int, document, chunk) -> dict:
    return {
        "marker": marker,
        "document_id": document.id,
        "document_title": document.title,
        "page_number": chunk.page_number,
        "chunk_id": chunk.id,
        "character_start": chunk.character_start,
        "character_end": chunk.character_end,
        "text": chunk.text,
        "relevance": 1.0,
    }


def _clip(text: str, limit: int) -> str:
    """`text` on one line, without table-row references ("Table 1, row 1:"),
    cut at a word boundary to `limit` characters."""
    text = text_rows.ROW_PREFIX.sub("", text or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "..."


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


@dataclass
class LibraryAnswer:
    text: str
    citations: list
    notes: list


def _failure(document) -> str:
    job = (
        ProcessingJob.objects.filter(document_version__document=document)
        .order_by("-id")
        .only("error_message")
        .first()
    )
    reason = _clip(getattr(job, "error_message", "") or "", 120)
    return f"**{document.title}**" + (f" ({reason})" if reason else "")


def list_answer(user, **scope) -> LibraryAnswer:
    documents = list(_scope(user, **scope))
    ready = [doc for doc in documents if doc.status == Document.Status.COMPLETED]
    waiting = [
        doc
        for doc in documents
        if doc.status
        in (
            Document.Status.UPLOADED,
            Document.Status.QUEUED,
            Document.Status.PROCESSING,
        )
    ]
    failed = [
        doc
        for doc in documents
        if doc.status in (Document.Status.FAILED, Document.Status.QUARANTINED)
    ]

    if not documents:
        return LibraryAnswer(
            "You have no documents to ask about yet. Upload one from the Documents "
            "page, and once it has been read you can ask questions about it here.",
            [],
            ["answered from the document library: no documents"],
        )

    lines, citations = [], []
    if ready:
        order = ", newest first" if len(ready) > 1 else ""
        lines.append(f"You can ask about {_plural(len(ready), 'document')}{order}:")
        lines.append("")
        for position, document in enumerate(ready[:LIST_LIMIT], start=1):
            insight = _insight(document)
            first = (insight.summary or [None])[0] if insight else None
            chunk = (
                _chunk_for(document, first.get("page_number"), first.get("start"))
                if first
                else _chunk_for(document)
            )
            pages = _pages(document)
            line = f"{position}. **{document.title}**"
            details = [_plural(pages, "page")] if pages else []
            details.append(f"uploaded {_uploaded(document)}")
            line += f" ({', '.join(details)})"
            if first:
                line += f": {_clip(first.get('text', ''), DESCRIPTION_CHARACTERS)}"
            if chunk is not None:
                marker = len(citations) + 1
                citations.append(_citation(marker, document, chunk))
                line += f" [{marker}]"
            lines.append(line)
        if len(ready) > LIST_LIMIT:
            lines.append(
                f"...and {len(ready) - LIST_LIMIT} more, listed on the Documents page."
            )
    else:
        lines.append("None of your documents is ready to ask about yet.")

    if waiting:
        lines.append("")
        lines.append(
            "Still being read: "
            + ", ".join(f"**{doc.title}**" for doc in waiting)
            + "."
        )
    if failed:
        lines.append("")
        lines.append(
            "Could not be read, so nothing in "
            + ("it" if len(failed) == 1 else "them")
            + " can be answered: "
            + "; ".join(_failure(doc) for doc in failed)
            + "."
        )
    if ready:
        lines.append("")
        lines.append("Ask about any of them by topic, or ask for a summary of one.")

    return LibraryAnswer(
        "\n".join(lines),
        citations,
        [
            f"answered from the document library: {_plural(len(ready), 'document')} listed"
        ],
    )


def summary_answer(document, *, recent: bool = False) -> LibraryAnswer:
    pages = _pages(document)
    details = [_plural(pages, "page")] if pages else []
    details.append(f"uploaded {_uploaded(document)}")
    opening = f"**{document.title}** ({', '.join(details)})"
    if recent:
        opening = f"The most recently uploaded document is {opening}."
    else:
        opening += "."

    insight = _insight(document)
    lines, citations = [opening, ""], []

    def cite(item) -> str:
        chunk = _chunk_for(document, item.get("page_number"), item.get("start"))
        if chunk is None:
            return ""
        marker = len(citations) + 1
        citations.append(_citation(marker, document, chunk))
        return f" [{marker}]"

    summary = (insight.summary if insight else [])[:SUMMARY_SENTENCES]
    points = (insight.key_points if insight else [])[:KEY_POINTS]
    if summary:
        lines.append("Summary:")
        for item in summary:
            lines.append(f"- {_clip(item.get('text', ''), 300)}{cite(item)}")
    if points:
        lines.append("")
        lines.append("Key points:")
        for item in points:
            lines.append(f"- {_clip(item.get('text', ''), 240)}{cite(item)}")
    if not summary and not points:
        # No brief yet (still being prepared, or briefs are switched off): the
        # opening of the document, as it is written.
        chunk = _chunk_for(document)
        if chunk is not None:
            lines.append("It opens with:")
            lines.append(f"> {_clip(chunk.text, 400)}" + cite({}))
    lines.append("")
    lines.append("Ask a question about it for more detail.")
    return LibraryAnswer(
        "\n".join(lines),
        citations,
        [f"answered from the document library: the brief of '{document.title}'"],
    )


def answer(user, question: str, **scope) -> LibraryAnswer | None:
    """The catalogue's answer to a library question, or None for any other."""
    found = intent(user, question, **scope)
    if found is None:
        return None
    if found.kind == "summary" and found.document is not None:
        return summary_answer(found.document, recent=found.recent)
    return list_answer(user, **scope)

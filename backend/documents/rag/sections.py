"""Which part of a document a passage belongs to, and what an abbreviation means.

Two failures with one cause: a passage is judged by its own words, and a
document's words about WHAT a passage is part of often sit somewhere else.

1. THE UNITS OF A COURSE ARE NOT NAMED AFTER THE COURSE. A syllabus gives the
   course title once, in a heading table ("DYNAMICS OF MACHINERY | Code |
   Credits"), and the next page lists "UNIT - I ... UNIT - V" under a bare
   "SYLLABUS". Asked for "the syllabus for dynamics of machinery", retrieval
   ranked those unit passages 21st-28th of the candidates and the answer
   stopped at the heading, or abstained (measured on the live site,
   2026-11-18). `running_titles` gives every passage the title of the table
   heading it follows; `matching_sections` finds the sections a question names
   and `expand` brings the whole section to the model.

2. THE QUESTION USES AN ABBREVIATION THE DOCUMENT DEFINES ONCE. "syllabus for
   mefa": MEFA appears only in an elective table, "Managerial Economics and
   Financial Accountancy (MEFA)", and the course itself never says MEFA.
   `expand_abbreviations` reads the definition from the user's own documents.

Nothing here changes a stored chunk or a citation. The title is shown to the
reranker and to the model beside the passage, the way a table's document title
already is (rag.tables.scoring_text), and a section's passages are real chunks
fetched through the same authorized queryset as search.
"""

import re
from collections import defaultdict

from django.conf import settings

from documents.processing.tables import text_rows

from .reranking import RerankedChunk, _terms

# How many passages of one section go to the model. A course is a heading, its
# outcomes and five or six units: about this many chunks.
DEFAULT_SECTION_CHUNKS = 14

# At most this many sections are brought in for one question; a question that
# names several is still one the budget has to hold.
MAX_SECTIONS = 2

# Headings that say what KIND of block follows, not what it is part of. A
# "SYLLABUS" heading must not replace the course title above it.
GENERIC_TITLES = frozenset(
    {
        "syllabus",
        "text books",
        "text book",
        "textbooks",
        "textbook",
        "reference books",
        "reference book",
        "references",
        "web resources",
        "course outcomes",
        "course objectives",
        "contents",
        "index",
        "summary",
        "notes",
        "total",
        "totals",
    }
)

# Words of a section title that name its category, not its subject; a question
# need not repeat them to be asking for the section.
CATEGORY_TERMS = frozenset(
    {
        "professional",
        "elective",
        "electives",
        "open",
        "core",
        "course",
        "year",
        "semester",
        "sem",
        "unit",
        "part",
        "lab",
        "laboratory",
        "theory",
        "pe",
        "oe",
    }
)

_ROMAN_OR_NUMBER = re.compile(r"^(?:[ivx]+|\d+)$")


def _title_of(context: str) -> str:
    """The table title in a chunk's `table_context`, or ''.

    The context of a labelled row is "title / section" when the table has
    both; only the title says what the page is about. A generic heading
    (SYLLABUS, TEXT BOOKS) says what kind of block it is, which is not a
    title.
    """
    title = (context or "").split(text_rows.CONTEXT_SEPARATOR)[0].strip()
    normal = " ".join(title.lower().split()).strip(":.")
    if not normal or normal in GENERIC_TITLES or normal.startswith("unit"):
        return ""
    if len(title) > 160:
        return ""
    return title


def running_titles(rows) -> dict:
    """chunk_index -> the table title in force at that chunk.

    `rows` are (chunk_index, metadata) in any order. A chunk that opens a
    titled table starts a section; every chunk after it belongs to that
    section until the next title. Chunks before the first title have none.
    """
    titles = {}
    current = ""
    for index, metadata in sorted(rows, key=lambda row: row[0]):
        own = _title_of((metadata or {}).get("table_context") or "")
        if own:
            current = own
        if current:
            titles[index] = current
    return titles


def _chunk_rows(content_ids) -> dict:
    """content_id -> [(chunk_index, chunk_id, metadata)], one query."""
    from documents.models import DocumentChunk

    found = defaultdict(list)
    queryset = DocumentChunk.objects.filter(document_content_id__in=list(content_ids))
    for content_id, index, chunk_id, metadata in queryset.values_list(
        "document_content_id", "chunk_index", "id", "metadata"
    ):
        found[content_id].append((index, chunk_id, metadata))
    return found


def annotate(items) -> None:
    """Give each item's chunk a `section_title` attribute.

    `items` have a `.chunk`. Called on what retrieval returned, before the
    reranker reads it, and again on any passage added after. Failure to read
    the sections never fails a question: the passages are simply unlabelled.
    """
    items = [item for item in items if getattr(item, "chunk", None) is not None]
    content_ids = {
        item.chunk.document_content_id
        for item in items
        if getattr(item.chunk, "document_content_id", None) is not None
    }
    if not content_ids:
        return
    try:
        rows = _chunk_rows(content_ids)
    except Exception:  # noqa: BLE001 - labels are an aid, never a gate
        return

    by_content = {
        content_id: running_titles((index, metadata) for index, _, metadata in found)
        for content_id, found in rows.items()
    }
    for item in items:
        chunk = item.chunk
        title = by_content.get(chunk.document_content_id, {}).get(chunk.chunk_index)
        if title:
            chunk.section_title = title


def section_of(chunk) -> str:
    return str(getattr(chunk, "section_title", "") or "")


def label(chunk) -> str:
    """The section as one short line for a prompt header."""
    return " ".join(section_of(chunk).split())[:120]


# --- which sections a question names -------------------------------------


def _distinctive(title: str) -> set:
    return {
        term
        for term in _terms(title)
        if term not in CATEGORY_TERMS and not _ROMAN_OR_NUMBER.match(term)
    }


def _squash(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


# A heading read off a page with its spaces lost ("APPLIEDTHERMODYNAMICS-II",
# as the live syllabus has it) is matched on its letters alone. Long enough
# that the letters cannot be inside an unrelated word by accident.
MIN_SQUASHED = 12

_TRAILING_NUMBER = re.compile(r"[\s\-–]+(?:[ivx]+|\d+)\s*$", re.I)


def names_section(question_terms: set, title: str, squashed_question: str = "") -> bool:
    """Whether the question says what the section is.

    Every distinctive term of the title is among the question's, and there
    are at least two of them - or one long one. A one-word title such as
    "Results" would otherwise be "named" by any question containing it.
    """
    wanted = _distinctive(title)
    if wanted and wanted <= question_terms:
        return len(wanted) >= 2 or max(len(term) for term in wanted) >= 7
    core = _squash(_TRAILING_NUMBER.sub("", title))
    return len(core) >= MIN_SQUASHED and core in squashed_question


def matching_sections(question: str, anchors) -> list:
    """(content_id, title) of the sections the question names, best first.

    Looked for among the documents the candidates came from: a section of a
    document retrieval never touched is not what this question is about.
    """
    question_terms = set(_terms(question))
    squashed = _squash(question)
    if not question_terms:
        return []

    content_ids = []
    for item in anchors:
        content_id = getattr(item.chunk, "document_content_id", None)
        if content_id is not None and content_id not in content_ids:
            content_ids.append(content_id)
    if not content_ids:
        return []

    try:
        rows = _chunk_rows(content_ids)
    except Exception:  # noqa: BLE001
        return []

    found = []
    for content_id in content_ids:
        titles = running_titles(
            (index, metadata) for index, _, metadata in rows.get(content_id, [])
        )
        for title in dict.fromkeys(titles.values()):
            if names_section(question_terms, title, squashed):
                found.append((content_id, title))
    return found


def expand(user, question: str, anchors, *, limit=None) -> list:
    """The passages of the sections `question` names, in document order.

    A passage retrieval already scored keeps its own RerankedChunk (and its
    score); the rest are new, flagged as ones a model judges. One copy of a
    section when the same file was uploaded more than once: the candidates'
    own order says which document the best passage was in, so the first
    content to name a section keeps it.
    """
    limit = (
        getattr(settings, "RAG_SECTION_CHUNKS", DEFAULT_SECTION_CHUNKS)
        if limit is None
        else limit
    )
    sections = []
    seen_titles = set()
    for content_id, title in matching_sections(question, anchors):
        key = " ".join(title.lower().split())
        if key in seen_titles:
            continue
        seen_titles.add(key)
        sections.append((content_id, title))
        if len(sections) >= MAX_SECTIONS:
            break
    if not sections:
        return []

    from documents.search import service as search_service

    rows = _chunk_rows({content_id for content_id, _ in sections})
    queryset, _ = search_service.authorized_chunks(user)
    known = {item.chunk.id: item for item in anchors}
    items = []
    for content_id, title in sections:
        titles = running_titles(
            (index, metadata) for index, _, metadata in rows.get(content_id, [])
        )
        indexes = sorted(index for index, found in titles.items() if found == title)
        if not indexes:
            continue
        chunks = {
            chunk.chunk_index: chunk
            for chunk in queryset.filter(
                document_content_id=content_id, chunk_index__in=indexes[:limit]
            )
            .select_related("document_version__document")
            .defer(*search_service.CANDIDATE_DEFERRED_FIELDS)
        }
        for index in indexes[:limit]:
            chunk = chunks.get(index)
            if chunk is None:
                continue
            if chunk.id in known:
                known[chunk.id].chunk.section_title = title
                items.append(known[chunk.id])
                continue
            chunk.section_title = title
            items.append(
                RerankedChunk(
                    chunk=chunk,
                    score=0.0,
                    signals={"model_judged": True, "section": title},
                )
            )
    return items


# --- abbreviations ---------------------------------------------------------

_CONNECTORS = frozenset({"and", "of", "the", "for", "in", "to", "on", "a", "an", "&"})
_ABBREVIATION = re.compile(r"\b[A-Za-z]{3,8}\b")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*|&")

# Words a question is made of that are never worth a database look: asking
# what "THE" stands for would scan every chunk for nothing.
_COMMON = frozenset(
    {
        "the",
        "and",
        "for",
        "what",
        "was",
        "are",
        "who",
        "how",
        "why",
        "when",
        "does",
        "did",
        "can",
        "you",
        "tell",
        "show",
        "give",
        "list",
        "about",
        "with",
        "from",
        "this",
        "that",
        "have",
        "has",
        "any",
        "all",
    }
)

MAX_ABBREVIATION_CANDIDATES = 6


def _full_form(text: str, short: str) -> str:
    """The words before "(SHORT)" whose initials spell SHORT, or ''.

    "Managerial Economics and Financial Accountancy (MEFA)" -> the five
    words: connectors do not count towards the initials but stay in the
    phrase. Only an exact spelling counts; a guess would put words in the
    question that the user never meant.
    """
    for match in re.finditer(r"\(\s*" + re.escape(short) + r"\s*\)", text):
        words = _WORD.findall(text[max(0, match.start() - 160) : match.start()])
        for size in range(len(short), len(short) + 4):
            if size > len(words):
                break
            phrase = words[-size:]
            initials = "".join(
                word[0].upper() for word in phrase if word.lower() not in _CONNECTORS
            )
            if initials == short and phrase[0].lower() not in _CONNECTORS:
                return " ".join(phrase)
    return ""


def expand_abbreviations(user, question: str) -> list:
    """Full forms of the abbreviations in `question`, as defined in the
    asker's own documents: "MEFA" -> "Managerial Economics and Financial
    Accountancy". One query, over the chunks the asker may read.

    A word is looked up only as an abbreviation - the document must have it
    in capitals in brackets, "(MEFA)" - so an ordinary word costs nothing and
    cannot be "expanded".
    """
    seen = []
    for word in _ABBREVIATION.findall(question or ""):
        if word.lower() in _COMMON or word.upper() in seen:
            continue
        seen.append(word.upper())
    seen = seen[:MAX_ABBREVIATION_CANDIDATES]
    if not seen:
        return []

    from documents.search import service as search_service

    try:
        queryset, _ = search_service.authorized_chunks(user)
        pattern = r"\(\s*(?:" + "|".join(re.escape(word) for word in seen) + r")\s*\)"
        texts = list(
            queryset.filter(text__regex=pattern).values_list("text", flat=True)[:40]
        )
    except Exception:  # noqa: BLE001 - a hint, never a gate
        return []

    forms = []
    for short in seen:
        for text in texts:
            full = _full_form(text, short)
            if full:
                if full.lower() not in {form.lower() for form in forms}:
                    forms.append(full)
                break
    return forms


def with_expansions(user, question: str):
    """(question to search with, the full forms added). The question itself,
    unchanged, when nothing needed saying."""
    forms = expand_abbreviations(user, question)
    if not forms:
        return question, []
    return f"{question} ({'; '.join(forms)})", forms

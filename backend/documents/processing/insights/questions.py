"""Questions this document can answer, written from what it actually holds.

Templates over evidence that has a location: a field the schema found, a
deadline, a table's header and first row, a heading, a key phrase. Each
candidate keeps the place its answer is (its "anchor"), and `check` - the
service's self-check against the document's own indexed chunks - decides
whether the question would actually retrieve that place. A question that
cannot is dropped rather than suggested: a suggestion that leads to "I
could not find that" is worse than a shorter list.
"""

import re
from dataclasses import dataclass

from .text import collapse

MIN_QUESTIONS = 3
MAX_QUESTIONS = 6
MAX_SUBJECT_WORDS = 8

TYPE_NOUN = {
    "CIRCULAR": "this circular",
    "REGULATION": "these regulations",
    "POLICY": "this policy",
    "SYLLABUS": "this course",
    "FORM": "this form",
}

# (document type, field) -> template. {value} is the field's value, {noun}
# the document's noun phrase. Only fields whose value makes a question.
FIELD_TEMPLATES = {
    ("CIRCULAR", "subject"): "What does the circular on {short} say?",
    ("CIRCULAR", "issuing_authority"): "Who issued this circular?",
    ("CIRCULAR", "issue_date"): "When was this circular issued?",
    ("REGULATION", "effective_date"): "When do these regulations take effect?",
    (
        "REGULATION",
        "academic_year",
    ): "Which academic year do these regulations apply to?",
    ("REGULATION", "approving_body"): "Who approved these regulations?",
    ("POLICY", "effective_date"): "When does this policy take effect?",
    ("POLICY", "review_date"): "When is this policy due for review?",
    ("SYLLABUS", "credits"): "How many credits is {course} worth?",
    ("SYLLABUS", "semester"): "In which semester is {course} taught?",
    ("SYLLABUS", "course_title"): "What topics does {short} cover?",
    ("FORM", "form_title"): "What is the {short} form for?",
}

HEADING_NUMBER = re.compile(
    r"^\s*(?:(?:clause|section|rule|article|unit|chapter|part)\s+[A-Z]?[\dIVX]+"
    r"(?:\.\d+)*[.:)]?|\d{1,2}(?:\.\d{1,2})*[.)]?|\(?[a-hivx]{1,4}\))\s*",
    re.IGNORECASE,
)
DEADLINE_LEAD = re.compile(
    r"^(?:last|due|closing)\s+date\s+for\s+|^deadline\s+for\s+", re.I
)


@dataclass(frozen=True)
class Candidate:
    question: str
    # (page_number, character offset) of the answer, or None.
    anchor: tuple | None
    source: str


def _short(value, words=MAX_SUBJECT_WORDS):
    parts = collapse(value).rstrip(".:;,").split()
    text = " ".join(parts[:words])
    return text


def _lower_first(text):
    if not text:
        return text
    first = text.split()[0]
    if first.isupper() and len(first) > 1:
        return text
    return text[0].lower() + text[1:]


def _finish(question):
    question = collapse(question).rstrip(" .?:;,")
    if not question:
        return ""
    return question[0].upper() + question[1:] + "?"


def candidates(*, document_type, fields, deadlines, tables, headings, phrases):
    """Every templated question, in preference order (unchecked)."""
    found = []

    for entity in deadlines:
        label = (entity.label or "").strip()
        lead = DEADLINE_LEAD.match(label)
        if lead:
            question = f"What is the last date for {_short(label[lead.end() :], 10)}"
        else:
            question = f"What is due by {collapse(entity.text)}"
        found.append(
            Candidate(_finish(question), (entity.page_number, entity.start), "deadline")
        )

    values = {name: value for name, value, *_ in fields}
    course = values.get("course_title") or values.get("course_code")
    for name, value, page, start, _end in fields:
        template = FIELD_TEMPLATES.get((document_type, name))
        if template is None:
            continue
        if "{course}" in template and not course:
            continue
        question = template.format(short=_short(value), course=_short(course or ""))
        found.append(Candidate(_finish(question), (page, start), "field"))

    for header, rows, page, start in tables:
        if len(header) < 2 or not rows:
            continue
        row = next((r for r in rows if r and r[0].strip() and r[0] != header[0]), None)
        column = next(
            (h for h in header[1:] if h and re.search(r"[A-Za-z]{3}", h)), None
        )
        if row is None or column is None or not re.search(r"[A-Za-z]", row[0]):
            continue
        question = (
            f"What is the {_lower_first(_short(column, 5))} for {_short(row[0], 6)}"
        )
        found.append(Candidate(_finish(question), (page, start), "table"))

    for segment in headings:
        title = HEADING_NUMBER.sub("", segment.text).strip(" :-")
        words = title.split()
        if not 1 <= len(words) <= 7 or not re.search(r"[A-Za-z]{3}", title):
            continue
        if title.isupper():
            title = title.title()
        question = f"What does the document say about {_lower_first(title)}"
        found.append(
            Candidate(
                _finish(question), (segment.page_number, segment.start), "heading"
            )
        )

    for phrase, _score in phrases:
        if len(phrase.split()) < 2:
            continue
        found.append(
            Candidate(
                _finish(f"What does the document say about {phrase}"), None, "phrase"
            )
        )

    return found


def choose(candidates_list, check, *, maximum=MAX_QUESTIONS):
    """Checked questions, varied by source: one of each kind before seconds."""
    chosen, seen = [], set()
    by_source = {}
    for candidate in candidates_list:
        by_source.setdefault(candidate.source, []).append(candidate)

    order = ["deadline", "field", "table", "heading", "phrase"]
    checks = 0
    for _ in range(MAX_QUESTIONS):
        progressed = False
        for source in order:
            queue = by_source.get(source, [])
            while queue:
                candidate = queue.pop(0)
                key = candidate.question.lower()
                if key in seen or len(candidate.question) > 140:
                    continue
                seen.add(key)
                checks += 1
                if check(candidate.question, candidate.anchor):
                    chosen.append(candidate.question)
                    progressed = True
                    break
            if len(chosen) >= maximum:
                return chosen, checks
        if not progressed:
            break
    return chosen, checks

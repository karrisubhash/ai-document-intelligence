"""Institutional document-type detection - no training, no model artifact.

Predicts the product's own taxonomy (Document.DocumentType: CIRCULAR,
REGULATION, POLICY, SYLLABUS, FORM) from evidence the platform already has,
so that a document uploaded as the default GENERAL can be typed - and, with
the type, have its fields extracted - without anyone choosing it.

Two kinds of evidence, combined into one score per type:

*Pattern votes.* Cues that say what kind of document this is: the heading
naming it ("CIRCULAR", "ACADEMIC REGULATIONS", "Course Syllabus"), phrases
that occur in one kind and rarely another ("this circular", "Unit III",
"for office use only"), and the field-extraction schema pack itself - a
circular number, a course code with credits, a review date are each found
by the pack's own patterns and vote for the type whose schema declares them.
Each cue counts once, with a weight that says how distinctive it is.

*Embedding prototypes* (only when the configured embedding provider is
semantic). A few sentences describing each type are embedded once per
process; the document's opening passages are embedded and averaged; the
cosine of that centroid against each type's prototype is turned into a
z-score across the types, and the best types get a bounded bonus. Under the
hashing baseline - the test configuration - this signal is simply absent
and the detector runs on patterns alone, by design rather than by failure.

The scores become probabilities with a softmax that includes a "none of
these" option at a fixed logit. That option is what makes abstention
principled: with no evidence, "none" dominates; one weak cue is not enough;
a heading that names the type, or several independent cues that agree, is.
The detector abstains below DOCUMENT_TYPE_MIN_CONFIDENCE, and an abstention
is stored as one (ClassificationResult.abstained) with the best guess and
every score on record - never as a label.

The embedding bonus is capped below what "none" needs to lose: semantics
alone can tip a document with some pattern evidence over the threshold, but
cannot type a document that no pattern recognises. That keeps the decision
explainable - every auto-applied type has at least one human-readable reason
in `metadata.evidence` - and keeps an uncalibrated similarity from deciding
anything on its own.
"""

import functools
import logging
import math
import re
import time
from dataclasses import dataclass
from itertools import islice

from django.conf import settings

from documents.models import DocumentContent

from ..field_extraction.matching import NOT_FOUND, find_fields
from ..field_extraction.rule_extractor import DEFAULT_PACK
from ..field_extraction.schemas import SchemaError, load_pack
from ..tables import text_rows
from .base import UNKNOWN_LABEL, ClassificationPrediction

logger = logging.getLogger("adi.classification")

DETECTOR_NAME = "institutional-type-detector"
# Bumped whenever cues, weights or the combination change, so a stored
# verdict can be traced to the rules that produced it.
DETECTOR_VERSION = "1.0.0"

# The types this detector can name. GENERAL and OTHER are fallbacks, never
# predictions: "no confident match" is an abstention, not a class.
TYPES = ("CIRCULAR", "REGULATION", "POLICY", "SYLLABUS", "FORM")

DEFAULT_MIN_CONFIDENCE = 0.6

# How much of the document is read. Type evidence is concentrated at the
# start - a heading, a number, a subject line - and reading a 500-page
# regulation in full would cost seconds for no better answer.
#
# The "head" is the document's title plus the first few lines of page 1:
# where a document names itself. Kept to lines rather than characters,
# because a fixed window reached into the body - "dealt with under the
# academic regulations" in a procedure's third paragraph read as a heading.
HEAD_LINES = 3
HEAD_CHARACTERS = 300
BODY_PAGES = 12
BODY_CHARACTERS = 40_000

# The logit of "none of these". With every type at score 0 it takes about
# 70% of the probability; a type needs a score of ~3.2 to reach 60% - a
# heading that names it (3.5), or two or three independent cues that agree.
NONE_LOGIT = 2.5

# "Course notes, not a regulation": a head cue preceded by a negation is
# not a heading naming the type.
NEGATION_BEFORE = re.compile(
    r"\b(?:not|no|non)[\s-]+(?:an?\s+|the\s+)?$", re.IGNORECASE
)

# The semantic bonus is weight * max(z, 0); a z-score across five types is
# at most ~1.79, so the bonus stays under the ~3.2 a type needs. See the
# module docstring for why that cap matters.
SEMANTIC_WEIGHT = 1.0
SEMANTIC_PASSAGES = 4
SEMANTIC_PASSAGE_CHARACTERS = 1_000

_FLAGS = re.IGNORECASE | re.MULTILINE


@dataclass(frozen=True)
class Cue:
    document_type: str
    # Why this counts, in words a reader can be shown.
    reason: str
    pattern: re.Pattern
    weight: float
    # "head": the title and the opening lines of page 1. "body": the pages
    # read (see BODY_PAGES).
    zone: str = "body"
    # Some cues only mean something in quantity: one "shall" is anywhere,
    # several are regulation language.
    min_count: int = 1


def _cue(document_type, reason, pattern, weight, zone="body", min_count=1):
    return Cue(
        document_type=document_type,
        reason=reason,
        pattern=re.compile(pattern, _FLAGS),
        weight=weight,
        zone=zone,
        min_count=min_count,
    )


CUES = (
    # --- CIRCULAR ------------------------------------------------------
    _cue("CIRCULAR", "the heading calls it a circular", r"\bcircular\b", 3.5, "head"),
    _cue(
        "CIRCULAR",
        "the heading calls it a notice or office order",
        r"\b(?:notice|notification|office\s+order|memorandum)\b",
        2.0,
        "head",
    ),
    _cue(
        "CIRCULAR",
        "it refers to itself as a circular",
        r"\bthis\s+circular\b|\bcircular\s+(?:no\b|number\b|ref)",
        2.0,
    ),
    _cue(
        "CIRCULAR",
        "it addresses all students or staff",
        r"\ball\s+(?:the\s+)?(?:students|staff|faculty|employees|heads\s+of\s+"
        r"(?:the\s+)?departments?)\s+(?:are|is)\s+(?:hereby\s+)?"
        r"(?:informed|requested|directed|instructed|notified)",
        1.5,
    ),
    _cue("CIRCULAR", "it supersedes an earlier notice", r"\bsupersedes?\b", 0.5),
    # --- REGULATION ----------------------------------------------------
    _cue(
        "REGULATION",
        "the heading calls it regulations",
        r"\bregulations?\b",
        3.5,
        "head",
    ),
    _cue(
        "REGULATION",
        "the heading calls it rules or bye-laws",
        r"\b(?:rules|bye-?laws?|statutes?|ordinances?)\b",
        3.0,
        "head",
    ),
    # Numbered clauses are how regulations are written, and how many
    # policies are too - so they lean towards a regulation without being
    # able to outvote a heading that says "policy".
    _cue(
        "REGULATION",
        "it is arranged in numbered clauses",
        r"^\s*(?:clause|rule|section|regulation)\s+[A-Z]?\d+(?:\.\d+)*\b",
        1.0,
    ),
    _cue(
        "REGULATION",
        "it is written in mandatory language",
        r"\b(?:shall|must)\b",
        0.5,
        min_count=4,
    ),
    _cue(
        "REGULATION",
        "it governs attendance, examinations or grading",
        r"\b(?:minimum\s+attendance|condonation|end[-\s]semester\s+examination|"
        r"grade\s+points?|cgpa|sgpa|promotion\s+to\s+the\s+next)\b",
        1.0,
    ),
    # --- POLICY --------------------------------------------------------
    _cue("POLICY", "the heading calls it a policy", r"\bpolicy\b", 3.5, "head"),
    _cue(
        "POLICY",
        "the heading calls it guidelines, a procedure or a code of conduct",
        r"\b(?:guidelines?|code\s+of\s+conduct|procedures?|charter)\b",
        2.5,
        "head",
    ),
    _cue("POLICY", "it refers to itself as a policy", r"\bthis\s+policy\b", 2.0),
    _cue(
        "POLICY",
        "it has purpose, scope or responsibilities sections",
        r"^\s*(?:(?:clause|section)\s+)?(?:[A-Z]?\d+(?:\.\d+)*\.?\s+)?"
        r"(?:purpose|scope|applicability|responsibilit(?:y|ies)|"
        r"policy\s+statement|objectives?)\s*[:.\n]",
        1.5,
        min_count=2,
    ),
    # --- SYLLABUS ------------------------------------------------------
    _cue(
        "SYLLABUS",
        "the heading calls it a syllabus or curriculum",
        r"\b(?:syllabus|syllabi|curriculum|course\s+(?:outline|structure|plan|"
        r"content))\b",
        3.5,
        "head",
    ),
    _cue(
        "SYLLABUS",
        "it is divided into units",
        r"^\s*unit\s*[-:.]?\s*(?:[ivx]{1,4}|\d{1,2})\b",
        1.5,
    ),
    _cue(
        "SYLLABUS",
        "it lists course objectives, outcomes or textbooks",
        r"\b(?:course\s+(?:objectives?|outcomes?)|learning\s+outcomes?|"
        r"text\s*books?|reference\s+books?|lecture\s+hours?)\b",
        1.5,
    ),
    _cue("SYLLABUS", "it gives an L-T-P scheme", r"\bL\s*[-/]?\s*T\s*[-/]?\s*P\b", 1.0),
    # --- FORM ----------------------------------------------------------
    _cue(
        "FORM",
        "the heading calls it a form",
        r"\b(?:application|registration|admission|claim|request|requisition|"
        r"declaration|enrol(?:l)?ment)\s+form\b|^\s*form\b",
        3.5,
        "head",
    ),
    _cue(
        "FORM",
        "it has blanks to fill in",
        r"_{5,}|\.{8,}|:\s*\[\s*\]",
        1.5,
        min_count=3,
    ),
    _cue(
        "FORM",
        "it has a signature line or an office-use box",
        r"\bsignature\s+of\s+(?:the\s+)?(?:applicant|candidate|student|"
        r"parent|guardian|employee)\b|\bfor\s+office\s+use(?:\s+only)?\b",
        2.0,
    ),
    _cue(
        "FORM",
        "it asks for the applicant's details",
        r"\bname\s+of\s+(?:the\s+)?(?:applicant|candidate|student|employee)\b|"
        r"\b(?:roll|registration|enrol(?:l)?ment)\s+(?:no|number)\b",
        1.0,
    ),
    _cue("FORM", "it carries a declaration", r"\bi\s+hereby\s+declare\b", 1.0),
)

# How much each schema-pack field votes for its type when the pack's own
# patterns locate it. Distinctive fields weigh more: a course code is close
# to proof of a syllabus, a "Date:" line is evidence of almost nothing.
# A field not listed here (a custom pack) votes FIELD_DEFAULT_WEIGHT.
FIELD_WEIGHTS = {
    ("CIRCULAR", "circular_number"): 2.5,
    ("CIRCULAR", "subject"): 1.0,
    ("CIRCULAR", "issue_date"): 0.5,
    ("CIRCULAR", "issuing_authority"): 0.5,
    ("REGULATION", "regulation_number"): 1.5,
    ("REGULATION", "academic_year"): 0.75,
    ("REGULATION", "approving_body"): 1.0,
    ("REGULATION", "effective_date"): 0.5,
    ("POLICY", "policy_number"): 2.5,
    ("POLICY", "review_date"): 1.5,
    ("POLICY", "effective_date"): 0.5,
    ("SYLLABUS", "course_code"): 2.0,
    ("SYLLABUS", "course_title"): 2.0,
    ("SYLLABUS", "credits"): 1.5,
    ("SYLLABUS", "semester"): 1.0,
    ("FORM", "form_number"): 2.5,
    # "Application for revaluation must be submitted..." matches this rule
    # in a regulation, so it counts for little on its own.
    ("FORM", "form_title"): 0.75,
}
FIELD_DEFAULT_WEIGHT = 1.0

# A few sentences per type, embedded once per process. Written to describe
# the kind of document, in the vocabulary such documents actually use.
PROTOTYPES = {
    "CIRCULAR": (
        "Circular issued by the office of the registrar informing all "
        "students and staff about a decision, with a circular number, a date "
        "and a subject line.",
        "Notice: all students are hereby informed that the following "
        "arrangements apply from the date of this circular.",
    ),
    "REGULATION": (
        "Academic regulations: rules on attendance, examinations, grading, "
        "credits, promotion and the award of the degree, in numbered clauses.",
        "The regulations approved by the academic council govern "
        "eligibility, evaluation and the conduct of examinations.",
    ),
    "POLICY": (
        "Institutional policy stating its purpose, scope, roles and "
        "responsibilities, procedures and review, with an effective date.",
        "This policy applies to all employees and students and sets out the "
        "principles and procedures the institution follows.",
    ),
    "SYLLABUS": (
        "Course syllabus with the course code, credits, course objectives, "
        "units of study, topics, textbooks, reference books and outcomes.",
        "Unit I introduction, Unit II and Unit III: the topics covered in "
        "this course, lecture hours and the list of experiments.",
    ),
    "FORM": (
        "Application form to be filled in by the candidate: name, roll "
        "number, department, date of birth, address, signature of the "
        "applicant and a box for office use only.",
        "Declaration: I hereby declare that the information furnished above "
        "is true. Signature of the student. Date.",
    ),
}


@functools.cache
def _load_pack(path: str):
    """The schema pack whose field patterns vote. Cached per path."""
    return load_pack(path or DEFAULT_PACK)


def min_confidence() -> float:
    value = getattr(settings, "DOCUMENT_TYPE_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)
    return min(max(float(value), 0.0), 1.0)


class InstitutionalTypeDetector:
    """Detects Document.DocumentType from content. See the module docstring."""

    name = DETECTOR_NAME
    version = DETECTOR_VERSION
    taxonomy = "document_type"

    def __init__(self):
        # Prototype vectors per embedding model, built on first use. A
        # prototype is only comparable to a passage embedded by the same
        # model, so the cache is keyed by the provider's identity.
        self._prototypes = {}

    # --- protocol ------------------------------------------------------

    def predict(self, *, document_content_id: int) -> ClassificationPrediction:
        content = DocumentContent.objects.select_related(
            "document_version__document"
        ).get(id=document_content_id)
        pages = list(
            content.pages.order_by("page_number").values_list("page_number", "text")[
                :BODY_PAGES
            ]
        )
        return self.detect(
            pages=pages,
            title=content.document_version.document.title,
            input_reference=f"document_content:{document_content_id}",
        )

    # --- detection -----------------------------------------------------

    def detect(
        self, *, pages, title="", input_reference=""
    ) -> ClassificationPrediction:
        started = time.monotonic()
        body_pages = _bounded(pages)
        body = "\n".join(text for _, text in body_pages)
        first_page = body_pages[0][1] if body_pages else ""
        head = _head(title, first_page)

        scores = dict.fromkeys(TYPES, 0.0)
        evidence = {name: [] for name in TYPES}

        for cue in CUES:
            if cue.zone == "head":
                hits = _affirmed_hits(cue.pattern, head, cue.min_count)
            else:
                found = islice(cue.pattern.finditer(body), cue.min_count)
                hits = sum(1 for _ in found)
            if hits >= cue.min_count:
                scores[cue.document_type] += cue.weight
                evidence[cue.document_type].append(cue.reason)

        pack_name = self._vote_with_pack(body_pages, scores, evidence)

        signals = ["patterns"]
        semantic = self._semantic_votes(body_pages, title, scores, evidence)
        if semantic is not None:
            signals.append("embeddings")

        probabilities, none_probability = _softmax(scores)
        best = max(TYPES, key=lambda name: (probabilities[name], scores[name]))
        confidence = probabilities[best]
        threshold = min_confidence()
        abstained = confidence < threshold or not evidence[best]

        metadata = {
            "scores": {name: round(value, 4) for name, value in probabilities.items()},
            "none": round(none_probability, 4),
            "raw_scores": {name: round(value, 3) for name, value in scores.items()},
            "evidence": {
                name: reasons for name, reasons in evidence.items() if reasons
            },
            "threshold": threshold,
            "abstained": abstained,
            # What it would have said, so an abstention can still be shown
            # as "best guess" and the threshold retuned against real data.
            "top_label": best,
            "signals": signals,
            "schema_pack": pack_name,
            "pages_considered": len(body_pages),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        if semantic is not None:
            metadata["semantic"] = semantic

        return ClassificationPrediction(
            label=UNKNOWN_LABEL if abstained else best,
            confidence=round(float(confidence), 4),
            model_name=self.name,
            model_version=self.version,
            input_reference=input_reference,
            metadata=metadata,
        )

    def _vote_with_pack(self, pages, scores, evidence) -> str:
        """Let the field-extraction pack's own patterns vote for their type."""
        path = getattr(settings, "DOCUMENT_FIELD_SCHEMA_PACK", "") or ""
        try:
            pack = _load_pack(str(path))
        except SchemaError as exc:
            # A broken pack is the field extractor's failure to report, not
            # this detector's: it still has its own cues to go on.
            logger.warning("type_detection.pack_unavailable", extra={"error": str(exc)})
            return ""

        # The pages as field extraction reads them: a field found only in the
        # rows of a table of many ("Course Code: 23CS101" in row 1 of twelve)
        # is not one the document has, so it is no evidence of its type.
        pages = [(number, text_rows.field_view(text or "")) for number, text in pages]
        for name in TYPES:
            schema = pack.for_document_type(name)
            if schema is None:
                continue
            for match in find_fields(schema, pages):
                if match.status == NOT_FOUND:
                    continue
                weight = FIELD_WEIGHTS.get(
                    (name, match.field_name), FIELD_DEFAULT_WEIGHT
                )
                scores[name] += weight
                evidence[name].append(f"{match.field_name.replace('_', ' ')} found")
        return f"{pack.name} {pack.version}"

    def _semantic_votes(self, pages, title, scores, evidence):
        """Prototype similarity, when the embedding provider encodes meaning.

        Returns what it measured (for the record), or None when the signal
        is unavailable - the hashing baseline, no provider, or a provider
        that failed. None of those is a reason to fail type detection.
        """
        from ..chunking.exceptions import (
            EmbeddingProviderNotConfigured,
            EmbeddingProviderUnavailable,
        )
        from ..chunking.service import get_provider

        try:
            provider = get_provider()
        except (EmbeddingProviderNotConfigured, EmbeddingProviderUnavailable):
            return None
        if not getattr(provider, "semantic", False):
            return None

        passages = _probe_passages(pages, title)
        if not passages:
            return None

        try:
            prototypes = self._prototype_vectors(provider)
            batch = provider.embed(passages)
        except Exception as exc:
            logger.warning(
                "type_detection.embedding_unavailable", extra={"error": str(exc)}
            )
            return None

        centroid = _normalize(_mean([v for v in batch.vectors if any(v)]))
        if centroid is None:
            return None

        cosines = {name: _dot(centroid, prototypes[name]) for name in TYPES}
        mean = sum(cosines.values()) / len(cosines)
        spread = math.sqrt(
            sum((c - mean) ** 2 for c in cosines.values()) / len(cosines)
        )
        spread = max(spread, 0.01)
        for name, cosine in cosines.items():
            z = (cosine - mean) / spread
            if z > 0:
                scores[name] += SEMANTIC_WEIGHT * z
                if z >= 1.0:
                    evidence[name].append("its content reads like one")

        return {
            "model": f"{provider.name} {provider.version}",
            "cosine": {name: round(value, 4) for name, value in cosines.items()},
            "passages": len(passages),
        }

    def _prototype_vectors(self, provider) -> dict:
        key = (provider.name, provider.version)
        cached = self._prototypes.get(key)
        if cached is not None:
            return cached

        names, texts = [], []
        for name in TYPES:
            for text in PROTOTYPES[name]:
                names.append(name)
                texts.append(text)
        vectors = provider.embed(texts).vectors

        grouped = {}
        for name, vector in zip(names, vectors, strict=True):
            grouped.setdefault(name, []).append(vector)
        prototypes = {name: _normalize(_mean(group)) for name, group in grouped.items()}
        self._prototypes[key] = prototypes
        return prototypes


# --- helpers -----------------------------------------------------------------


def _head(title, first_page) -> str:
    """The title and the first HEAD_LINES non-empty lines of page 1."""
    lines = [line.strip() for line in (first_page or "").splitlines() if line.strip()]
    opening = "\n".join(lines[:HEAD_LINES])[:HEAD_CHARACTERS]
    return f"{(title or '').strip()}\n{opening}"


def _affirmed_hits(pattern, text, wanted) -> int:
    """Matches not preceded by a negation ("not a regulation")."""
    hits = 0
    for match in pattern.finditer(text):
        if NEGATION_BEFORE.search(text[max(0, match.start() - 12) : match.start()]):
            continue
        hits += 1
        if hits >= wanted:
            break
    return hits


def _bounded(pages):
    """The pages read, cut off at BODY_CHARACTERS in total."""
    kept, used = [], 0
    for number, text in pages[:BODY_PAGES]:
        if used >= BODY_CHARACTERS:
            break
        text = text or ""
        kept.append((number, text[: BODY_CHARACTERS - used]))
        used += len(text)
    return kept


def _probe_passages(pages, title):
    """The title and opening, plus passages spread through what was read."""
    text = "\n".join(page for _, page in pages).strip()
    if not text:
        return []
    size = SEMANTIC_PASSAGE_CHARACTERS
    passages = [f"{title}\n{text[:size]}".strip()]
    rest = text[size:]
    if rest:
        step = max(len(rest) // SEMANTIC_PASSAGES, size)
        for start in range(0, len(rest), step):
            passage = rest[start : start + size].strip()
            if passage:
                passages.append(passage)
            if len(passages) > SEMANTIC_PASSAGES:
                break
    return passages


def _softmax(scores):
    """Probabilities per type, with "none of these" at NONE_LOGIT."""
    top = max([NONE_LOGIT, *scores.values()])
    exps = {name: math.exp(value - top) for name, value in scores.items()}
    none = math.exp(NONE_LOGIT - top)
    total = none + sum(exps.values())
    return {name: value / total for name, value in exps.items()}, none / total


def _mean(vectors):
    vectors = [list(vector) for vector in vectors]
    if not vectors:
        return None
    width = len(vectors[0])
    return [sum(vector[i] for vector in vectors) / len(vectors) for i in range(width)]


def _normalize(vector):
    if vector is None:
        return None
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return None
    return [value / norm for value in vector]


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right, strict=True))

"""The answer-quality review queue: what people said about answers.

GET /api/v1/admin/feedback/?rating=INCORRECT,NOT_HELPFUL&since=2026-10-01&page=1

Feedback was write-only until this existed: a user who flagged an answer as
incorrect reached nobody. Here an administrator reads the ratings given on
answers in their scope - a department administrator their department's
conversations (Conversation.department, stamped when the conversation was
created), a system administrator every one - newest first.

WHAT AN ITEM SHOWS, AND WHAT IT DOES NOT. Conversations are owner-only, and
the audit log promises never to record question text. A review queue sits
between those two promises, so it shows the least that still lets someone
act on a complaint:

* Never who rated. The queue judges answers, not people.
* No time finer than the day the rating was given (`rated_on`, a day in
  ANALYTICS_TIME_ZONE like every daily series). ChatView writes a CHAT
  audit row under the asker's name within milliseconds of each answer,
  administrators read the audit log, and only the asker can rate an
  answer - so an exact answer or rating time named the rater. Nor the
  answer's id: ids are handed out in order, so it is a clock to anyone who
  can set it beside their own messages. (The item's own id stays - a list
  needs a key - and tells no more than the order, newest first, does.)
  And `since` is read as a day: a bound to the second, asked again and
  again, would say to the second when a rating was given.
* The answer, as an excerpt - the platform's own output, which is what is
  being judged. Withheld from a department administrator when the answer
  cites a document outside their department (or one since deleted, whose
  department can no longer be checked): a member's own document elsewhere
  is not theirs to read, and an excerpt quotes it.
* The question, only to a system administrator and only on a flag
  (INCORRECT or NOT_HELPFUL): the user asked for that answer to be looked
  at, and the operator who can already read every document is who looks.
  A department administrator - often the asker's own head of department -
  never sees it, and a HELPFUL rating never carries it.
* The comment, which the user wrote for exactly this reader.
"""

from datetime import datetime, time

from django.db.models import OuterRef, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import exceptions
from rest_framework.generics import ListAPIView

from accounts.permissions import IsAdministrator
from documents.api import discovery
from documents.models import Document, Feedback, Message

# A rating that asks for the answer to be looked at.
FLAGS = (Feedback.Rating.INCORRECT, Feedback.Rating.NOT_HELPFUL)

EXCERPT_LENGTH = 280
# Cited documents named per item, for linking to the sources.
DOCUMENTS_PER_ITEM = 3


def department_scope(user):
    """None for a system administrator (no restriction), else their department.

    The boundary every administration read model uses
    (operations.views._department_scope), imported late: operations.views
    imports this package's analytics for the dashboard.
    """
    from .views import _department_scope

    return _department_scope(user)


def day_zone():
    """Where a rating's day begins: the analytics time zone, so `rated_on`
    is the day the analytics feedback series counts it under. Imported
    late, as above: analytics imports this module."""
    from .analytics import analytics_zone

    return analytics_zone()


def since_day(value: str) -> datetime:
    """`since` as the first instant of the day it names, in day_zone().

    A date, or a datetime read as its day there: an exact bound would put
    back, one request at a time, the rating time the items leave out.
    Malformed or impossible values are a 400, as for any date bound.
    """
    instant = discovery.parse_boundary(value, "since", end_of_day=False)
    zone = day_zone()
    try:
        # A bare date names the day itself; parse_boundary put its midnight
        # in TIME_ZONE, which need not be the analytics zone.
        day = parse_date(value) or timezone.localdate(instant, zone)
    except OverflowError:
        # 0001-01-01T00:00+05:00 has no day in UTC.
        raise exceptions.ValidationError({"since": ["Out of range."]}) from None
    return datetime.combine(day, time.min, tzinfo=zone)


def excerpt(text: str, limit: int = EXCERPT_LENGTH) -> str:
    """The first `limit` characters, whitespace collapsed, cut at a word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return f"{cut}…"


def cited_document_ids(citations) -> list[int]:
    """The documents an answer cites, first citation first, each once.

    Citations are written by the answer pipeline, but a JSON column holds
    whatever was put in it, and one malformed row must not take the whole
    queue down with it.
    """
    ids = []
    for citation in citations if isinstance(citations, list) else []:
        value = citation.get("document_id") if isinstance(citation, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value not in ids:
            ids.append(value)
    return ids


def answer_source(message) -> str:
    """ "documents", "general" or "none", as ChatView recorded it.

    Answers from before the source was recorded have none: an abstention
    then had no source, and anything else was from the documents.
    """
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    source = metadata.get("source")
    if source in ("documents", "general", "none"):
        return source
    return "none" if message.abstained else "documents"


def with_question(queryset):
    """Annotate each feedback row with the question its answer replied to.

    The user turn just before the answer in the same conversation: ChatView
    writes the two together, question first, in one transaction. One
    correlated subquery rather than a query per row.
    """
    question = (
        Message.objects.filter(
            conversation_id=OuterRef("message__conversation_id"),
            role=Message.Role.USER,
            id__lt=OuterRef("message_id"),
        )
        .order_by("-id")
        .values("content")[:1]
    )
    return queryset.annotate(question_text=Subquery(question))


def scoped_feedback(department):
    """Feedback on answers in `department`'s conversations (None: all)."""
    queryset = Feedback.objects.select_related("message__conversation")
    if department is not None:
        queryset = queryset.filter(message__conversation__department=department)
    return queryset


def feedback_items(rows, *, department) -> list[dict]:
    """Review-queue items for feedback rows, in the order given.

    `rows` come from scoped_feedback(), annotated by with_question() when
    the caller is a system administrator (department None). Two queries
    however many rows: the rows, and the documents their answers cite.
    Dated to the day only - see the module docstring.
    """
    rows = list(rows)
    zone = day_zone()
    cited = {row.id: cited_document_ids(row.message.citations) for row in rows}
    documents = {
        document.id: document
        for document in Document.objects.filter(
            id__in={i for ids in cited.values() for i in ids}
        ).only("id", "title", "department")
    }

    items = []
    for row in rows:
        message = row.message
        named = [
            documents[i]
            for i in cited[row.id]
            if i in documents
            and (department is None or documents[i].department == department)
        ]
        withheld = department is not None and len(named) < len(cited[row.id])
        items.append(
            {
                "id": row.id,
                "rating": row.rating,
                "comment": row.comment,
                "rated_on": timezone.localdate(row.created_at, zone),
                "department": message.conversation.department,
                # None: not shown to this reader. "" for a flag whose
                # question cannot be found (an answer with no question).
                "question": (
                    (getattr(row, "question_text", None) or "")
                    if department is None and row.rating in FLAGS
                    else None
                ),
                "answer_excerpt": "" if withheld else excerpt(message.content),
                "answer_withheld": withheld,
                # As judged: Feedback snapshots these when it is given.
                "abstained": row.answer_abstained,
                "abstention_reason": message.abstention_reason,
                "citations": row.answer_citations,
                "source": answer_source(message),
                "generative": message.generative,
                "model": message.model_version or message.model_name,
                "support_ratio": message.support_ratio,
                "documents": [
                    {"id": document.id, "title": document.title}
                    for document in named[:DOCUMENTS_PER_ITEM]
                ],
            }
        )
    return items


def parse_ratings(value: str) -> list[str]:
    """`rating=INCORRECT,NOT_HELPFUL` - one rating or several, validated."""
    ratings = [part.strip().upper() for part in value.split(",") if part.strip()]
    unknown = [rating for rating in ratings if rating not in Feedback.Rating.values]
    if unknown:
        raise exceptions.ValidationError(
            {"rating": [f"Must be one or more of {', '.join(Feedback.Rating.values)}."]}
        )
    return ratings


class FeedbackListView(ListAPIView):
    """GET /api/v1/admin/feedback/ - the review queue, newest first.

    Filters: `rating` (one or comma-separated), `since` (a date, or an ISO
    datetime read as its day - see since_day). Paginated like every list
    (`page`).
    """

    permission_classes = [IsAdministrator]

    def get_queryset(self):
        department = department_scope(self.request.user)
        queryset = scoped_feedback(department).order_by("-created_at", "-id")
        params = self.request.query_params
        if params.get("rating", "").strip():
            queryset = queryset.filter(rating__in=parse_ratings(params["rating"]))
        since = params.get("since", "").strip()
        if since:
            queryset = queryset.filter(created_at__gte=since_day(since))
        if department is None:
            queryset = with_question(queryset)
        return queryset

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(
            feedback_items(page, department=department_scope(request.user))
        )

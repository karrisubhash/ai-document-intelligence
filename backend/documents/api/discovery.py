"""Search, filtering and ordering for GET /api/documents/ (§16, §22).

At the corpus size this platform targets, paging through hundreds of pages
by hand is not discovery. This module is the part of the listing endpoint
that lets a caller narrow down to what they are looking for - by title or
current-file name, by processing status, type, department, owner, or a
created/updated date range - and choose how the results are ordered.

AUTHORIZATION IS NEVER NEGOTIATED HERE. Every function in this module takes
an already-scoped queryset - the output of `authorization.visible_documents`
- and only narrows it further with `.filter()`. Django ANDs successive
`.filter()` calls onto the same queryset, so there is no combination of
search term, filter value or ordering field that can widen the result set
back past the authorization boundary; the boundary is structural, not a
value this module could get wrong. See `apply()`.

ORDERING NEVER TAKES A RAW STRING TO THE ORM. `ordering=owner__profile__x`
or `ordering=__class__` are exactly the kind of thing an allowlist exists to
refuse - see ORDERING_FIELDS and `resolve_ordering()`.
"""

from datetime import datetime, time

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import ValidationError

from documents.models import Document

# --- Ordering ---------------------------------------------------------

# Public name -> model field. Deliberately not every column on the model:
# `department` and `owner` are exposed as FILTERS, not sort keys, because
# sorting by a free-text or foreign-key column has no natural direction a
# caller would expect ("alphabetical by department" is a filter's job, a
# results table just needs SOME stable key). Every field here already
# appears in DocumentSerializer, so an ordering value never implies
# something about a document the caller could not otherwise see.
ORDERING_FIELDS = {
    "id": "id",
    "title": "title",
    "status": "status",
    "document_type": "document_type",
    "created_at": "created_at",
    "updated_at": "updated_at",
}

# Matches Document.Meta.ordering, so a request with no `ordering` behaves
# exactly as the endpoint always has.
DEFAULT_ORDERING = ("-created_at",)

# A caller sorting by five things at once is not browsing, and each extra
# key is an extra thing `resolve_ordering` must validate - three is already
# more than the UI this is built for will ever send.
MAX_ORDERING_FIELDS = 3


def resolve_ordering(raw: str) -> tuple:
    """Turn a comma-separated `ordering` value into safe `.order_by()` args.

    Every token must name a key in ORDERING_FIELDS, with an optional
    leading `-` for descending - the same convention DRF's own
    OrderingFilter uses, so it behaves the way an API consumer already
    expects. Anything else is a 400, not a silently-ignored no-op: DRF's
    built-in OrderingFilter falls back to the default ordering on an
    unrecognised field, which is exactly wrong here - a caller who
    misspelled `ordreing` deserves to be told, not served page 1 sorted a
    way they did not ask for and did not notice.
    """
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        raise ValidationError({"ordering": ["No ordering fields given."]})
    if len(tokens) > MAX_ORDERING_FIELDS:
        raise ValidationError(
            {"ordering": [f"At most {MAX_ORDERING_FIELDS} ordering fields."]}
        )

    resolved = []
    for token in tokens:
        descending = token.startswith("-")
        name = token[1:] if descending else token
        field = ORDERING_FIELDS.get(name)
        if field is None:
            raise ValidationError(
                {
                    "ordering": [
                        f"'{name}' is not an ordering field. Choose from: "
                        + ", ".join(sorted(ORDERING_FIELDS))
                    ]
                }
            )
        resolved.append(f"-{field}" if descending else field)

    return tuple(resolved)


def _with_tiebreak(ordering: tuple) -> tuple:
    """Append `id` so two rows can never compare equal.

    `created_at` has microsecond precision but is not guaranteed unique -
    a bulk import, a seed script, or simply two uploads landing in the same
    tick can tie. Left untied, Postgres is free to return matching rows in
    any order it likes on different executions of the identical query, and
    a page boundary that falls inside a tied group can silently skip or
    repeat a row between one page fetch and the next. Ordering by anything
    is not "deterministic" unless the LAST key is unique.
    """
    last = ordering[-1] if ordering else ""
    if last.lstrip("-") == "id":
        return ordering
    tiebreak = "-id" if last.startswith("-") else "id"
    return (*ordering, tiebreak)


# --- Choice filters -----------------------------------------------------

STATUS_VALUES = {choice for choice, _ in Document.Status.choices}
DOCUMENT_TYPE_VALUES = {choice for choice, _ in Document.DocumentType.choices}


# --- Date/datetime range filters -----------------------------------------


def parse_boundary(value: str, field_name: str, *, end_of_day: bool) -> datetime:
    """A `created_after`/`created_before`-style bound, from either an ISO
    datetime or a plain date.

    A full ISO-8601 datetime (`2025-01-31T00:00:00Z`) is used exactly as
    given - the caller named a precise instant. A bare date
    (`2025-01-31`) has no instant to be exact about, so it is read as
    "the whole named day": `end_of_day=False` (an `_after` bound) resolves
    to that day's first instant, `end_of_day=True` (a `_before` bound) to
    its last - which is what makes `created_after=2025-01-01
    &created_before=2025-01-31` mean "all of January" rather than
    excluding the 31st by a technicality nobody asked for.

    THE BARE-DATE CHECK MUST RUN FIRST. `parse_date` is tried before
    `parse_datetime`, not after - Django's `parse_datetime("2025-01-31")`
    does not reject a bare date and fall through the way a name like
    "parse A DATETIME" suggests. It succeeds, silently defaulting the
    missing time to midnight, so a naive first attempt at this function
    (parse_datetime, then fall back to parse_date) accepted a bare
    `created_before` date but resolved it to 00:00:00 - i.e. to
    `time.min` regardless of `end_of_day` - which made the boundary
    EXCLUDE every document created on the named day instead of including
    it. Caught by
    DateRangeFilterTests.test_created_before_a_bare_date_is_inclusive_of_the_named_day.
    """
    parsed_date = parse_date(value)
    if parsed_date is not None:
        naive = datetime.combine(parsed_date, time.max if end_of_day else time.min)
        return timezone.make_aware(naive)

    parsed_datetime = parse_datetime(value)
    if parsed_datetime is not None:
        if timezone.is_naive(parsed_datetime):
            parsed_datetime = timezone.make_aware(parsed_datetime)
        return parsed_datetime

    raise ValidationError(
        {
            field_name: [
                f"'{value}' is not a valid date or datetime. Use YYYY-MM-DD "
                "or ISO 8601 (e.g. 2025-01-31T00:00:00Z)."
            ]
        }
    )


# --- Applying a validated query ------------------------------------------


def apply(queryset, data: dict):
    """Narrow an ALREADY AUTHORIZATION-SCOPED queryset and order it.

    `data` is `DocumentListQuerySerializer.validated_data` - see
    documents.api.serializers. Never call this with an unscoped queryset;
    see the module docstring for why that is safe to rely on rather than
    merely conventional.
    """
    search = data.get("search") or ""
    if search:
        # A forward FK join (current_version) can match at most one version
        # row per document, so this OR can never multiply a document into
        # two result rows - no .distinct() needed, and none of the
        # composed filters below can change that, since they only ever
        # narrow further.
        queryset = queryset.filter(
            Q(title__icontains=search) | Q(current_version__file_name__icontains=search)
        )

    if data.get("status"):
        queryset = queryset.filter(status=data["status"])

    if data.get("document_type"):
        queryset = queryset.filter(document_type=data["document_type"])

    if data.get("department"):
        queryset = queryset.filter(department=data["department"])

    if data.get("owner_id") is not None:
        queryset = queryset.filter(owner_id=data["owner_id"])

    if data.get("created_after") is not None:
        queryset = queryset.filter(created_at__gte=data["created_after"])
    if data.get("created_before") is not None:
        queryset = queryset.filter(created_at__lte=data["created_before"])
    if data.get("updated_after") is not None:
        queryset = queryset.filter(updated_at__gte=data["updated_after"])
    if data.get("updated_before") is not None:
        queryset = queryset.filter(updated_at__lte=data["updated_before"])

    archived = data.get("archived") or "false"
    if archived == "false":
        queryset = queryset.filter(archived_at__isnull=True)
    elif archived == "true":
        queryset = queryset.filter(archived_at__isnull=False)

    ordering = _with_tiebreak(data.get("ordering") or DEFAULT_ORDERING)
    return queryset.order_by(*ordering)

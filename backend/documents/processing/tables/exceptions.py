"""The three-way split every pluggable capability here uses.

Not configured, unavailable and failed are different operational facts and
lead to different actions: skip the stage, retry the job, or fail it
permanently. Collapsing them into one exception is how a missing setting
ends up being retried forever and a transient outage ends up recorded as a
document that cannot be processed.
"""


class TableDetectionError(Exception):
    """The detector ran and failed on this input. Permanent: the same page
    detected again fails the same way."""


class TableDetectorNotConfigured(TableDetectionError):
    """No detector is configured. The stage records SKIPPED - there is no
    structure to find and nothing went wrong."""


class TableDetectorUnavailable(TableDetectionError):
    """The detector is configured but could not be loaded or reached.
    Transient: worth retrying, and not the document's fault."""

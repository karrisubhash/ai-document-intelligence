class ClassificationError(Exception):
    """Base for classification failures."""


class ClassifierNotConfigured(ClassificationError):
    """No classifier is wired up.

    Raised rather than silently falling back to a guess: a fabricated
    label is worse than no label, and the pipeline treats this as
    "classification unavailable", not as a document failure.
    """


class ClassifierUnavailable(ClassificationError):
    """A configured classifier could not be loaded or reached.

    Distinct from ClassifierNotConfigured: something is wired up but is
    temporarily failing, which may be worth retrying.
    """

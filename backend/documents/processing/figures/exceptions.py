"""Not configured, unavailable, failed - see tables/exceptions.py for why
these are three exceptions rather than one."""


class FigureDetectionError(Exception):
    """The detector ran and failed on this input. Permanent."""


class FigureDetectorNotConfigured(FigureDetectionError):
    """No detector configured. The stage skips that half of its work."""


class FigureDetectorUnavailable(FigureDetectionError):
    """Configured but not loadable. Transient, and not the document's fault."""

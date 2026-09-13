class ScanError(Exception):
    """Base for scanning failures."""


class ScannerNotConfigured(ScanError):
    """No scanner is wired up.

    The stage records SKIPPED. Deliberately NOT a failure: a deployment may
    scan at the storage layer or the gateway instead, and refusing every
    upload because this particular hook is empty would be its own outage.
    What must never happen is a document being reported as scanned when it
    was not - hence a distinct SKIPPED event rather than a silent pass.
    """


class ScannerUnavailable(ScanError):
    """A configured scanner could not be reached or could not read the file.

    Retryable, and the document does NOT proceed in the meantime. Letting an
    unscanned file through because the scanner was briefly down would make
    the control worthless exactly when it is being stressed.
    """


class DocumentInfected(ScanError):
    """The scanner found something. The document is quarantined."""

    def __init__(self, message, findings=()):
        super().__init__(message)
        self.findings = tuple(findings)

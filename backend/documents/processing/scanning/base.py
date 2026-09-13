from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class Verdict:
    CLEAN = "CLEAN"
    # Something was found. The document is quarantined, never processed.
    INFECTED = "INFECTED"
    # Something looks wrong but is not conclusive - an embedded macro is
    # not malware, it is a capability. Recorded and allowed through, so the
    # operator can decide policy from evidence rather than from a guess.
    SUSPICIOUS = "SUSPICIOUS"


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    detail: str
    # Where in the file, when the scanner can say. Kept because "a macro
    # exists" and "a macro exists at this offset in this part" are very
    # different amounts of help to whoever has to triage it.
    location: str = ""


@dataclass
class ScanResult:
    verdict: str
    findings: tuple = ()
    scanner_name: str = ""
    scanner_version: str = ""
    # Signature database or ruleset version. A scan is only meaningful
    # alongside what it was scanning *with*, and a clean verdict from last
    # year's rules is not the same claim as a clean verdict from today's.
    ruleset_version: str = ""
    duration_ms: int = 0
    bytes_scanned: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def infected(self) -> bool:
        return self.verdict == Verdict.INFECTED

    @property
    def suspicious(self) -> bool:
        return self.verdict == Verdict.SUSPICIOUS


@runtime_checkable
class DocumentScanner(Protocol):
    """The contract between the pipeline and any malware scanner.

    Implementations may be a heuristic rule set, a ClamAV daemon or a
    cloud scanning service. The pipeline is not allowed to care, and in
    particular it must not assume a scan is fast: a remote scanner is a
    network call, which is why ScannerUnavailable is a retryable condition
    rather than a reason to let a document through unscanned.
    """

    name: str
    version: str
    ruleset_version: str

    def scan(self, file_path: str) -> ScanResult: ...

from documents.observability import telemetry
from documents.processing.context import StageResult
from documents.processing.errors import ErrorCode, StageError, TransientError
from documents.processing.scanning import (
    DocumentInfected,
    ScannerNotConfigured,
    ScannerUnavailable,
    scan_file,
)


def run(context) -> StageResult:
    """Scan the stored file before anything parses it.

    Placed after VALIDATION and before TEXT_EXTRACTION deliberately.
    Validation has already established the file is a supported type of a
    sane size, which is cheap; extraction is the step that hands bytes to
    pypdf, python-docx and Tesseract, which is where a malicious file would
    get its chance. "Malware scanning before trusted processing" in section
    13 means exactly this boundary.
    """
    try:
        result = scan_file(context.file_path)
    except ScannerNotConfigured as exc:
        # Honest skip. The event says the file was NOT scanned rather than
        # letting a reader assume a clean bill of health.
        return StageResult(message=str(exc), skipped=True)
    except ScannerUnavailable as exc:
        # The document does not proceed. An unscanned file passing through
        # because the scanner was down defeats the control precisely when
        # it matters.
        raise TransientError(str(exc)) from exc

    findings = [
        {
            "rule": finding.rule,
            "severity": finding.severity,
            "detail": finding.detail,
            "location": finding.location,
        }
        for finding in result.findings
    ]

    metadata = {
        "verdict": result.verdict,
        "scanner": result.scanner_name,
        "scanner_version": result.scanner_version,
        "ruleset_version": result.ruleset_version,
        "bytes_scanned": result.bytes_scanned,
        "scan_ms": result.duration_ms,
        "findings": findings,
        **result.metadata,
    }

    telemetry.scan_verdict(
        result.verdict,
        document_id=context.document_version.document_id,
        findings=len(findings),
    )

    if result.infected:
        raise DocumentInfected(
            "; ".join(f"{f.rule}: {f.detail}" for f in result.findings),
            findings=result.findings,
        )

    if result.suspicious:
        # Allowed through, on the record. A macro is a capability, not proof
        # of intent, and blocking every macro-bearing document would make
        # the platform unusable for the institutions that mail them around.
        return StageResult(
            message=(
                f"{len(findings)} finding(s) noted by {result.scanner_name}; "
                f"not blocking"
            ),
            metadata=metadata,
        )

    return StageResult(
        message=(
            f"Clean ({result.scanner_name} {result.ruleset_version}, "
            f"{result.bytes_scanned} bytes)"
        ),
        metadata=metadata,
    )

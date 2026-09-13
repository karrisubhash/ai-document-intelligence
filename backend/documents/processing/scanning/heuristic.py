"""A heuristic document scanner that needs no daemon and no network.

This is NOT an antivirus. It has no signature database, it will not catch a
novel dropper, and a determined attacker will walk past it. Saying so
plainly matters more than the rules it does implement, because a security
control that is trusted beyond its actual reach is worse than none.

What it does catch is the realistic file-upload abuse for a document
platform: a document type that is secretly an executable, an Office file
carrying a macro, a PDF that runs JavaScript or launches a program, and the
EICAR test string. Those are cheap, deterministic checks on bytes the
platform is about to hand to a parser anyway.

The point of shipping it is the same as elsewhere in this project: the
mechanism - a scan stage, a quarantine state, a pluggable contract, an
audit trail - is built and exercised, and pointing `DOCUMENT_SCANNER` at
ClamAV later changes one setting rather than a subsystem.
"""

import re
import time
import zipfile
from pathlib import Path

from .base import Finding, ScanResult, Verdict

# The industry-standard harmless test file. Every scanner is expected to
# detect it, so its presence proves the scan path actually runs end to end
# without anyone needing real malware on their machine.
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

# Executable headers. A file that the client called a .pdf and that opens
# with one of these is not a mislabelled document, it is a disguised
# program - the single clearest signal available at this layer.
EXECUTABLE_SIGNATURES = (
    (b"MZ", "PE/DOS executable (MZ)"),
    (b"\x7fELF", "ELF executable"),
    (b"\xca\xfe\xba\xbe", "Mach-O fat binary"),
    (b"\xcf\xfa\xed\xfe", "Mach-O 64-bit"),
    (b"#!/bin/sh", "shell script"),
    (b"#!/bin/bash", "shell script"),
    (b"#!/usr/bin/env", "interpreted script"),
)

# OOXML parts that carry executable content.
OOXML_MACRO_PARTS = (
    "word/vbaProject.bin",
    "xl/vbaProject.bin",
    "ppt/vbaProject.bin",
    "word/vbaData.xml",
)

# PDF constructs that do something rather than describe something.
PDF_ACTIVE_CONTENT = (
    (re.compile(rb"/JavaScript\b"), "PDF JavaScript action"),
    (re.compile(rb"/JS\b"), "PDF JavaScript stream"),
    (re.compile(rb"/Launch\b"), "PDF Launch action (runs an external program)"),
    (re.compile(rb"/EmbeddedFile\b"), "PDF embedded file"),
    (re.compile(rb"/OpenAction\b"), "PDF automatic action on open"),
    (re.compile(rb"/AA\b"), "PDF additional-actions dictionary"),
)

# Read in bounded chunks: a scanner that loads a 50 MB upload into memory to
# look for a 68-byte string is its own denial of service.
CHUNK = 1 << 20

# Overlap between chunks so a signature straddling a boundary is still seen.
OVERLAP = 256


class HeuristicScanner:
    """Deterministic, offline structural checks. Not an antivirus."""

    name = "heuristic-scanner"
    version = "0.1.0"
    # Bumped whenever a rule changes, so a stored CLEAN verdict says which
    # rules produced it.
    ruleset_version = "2026-09-rules-1"

    def scan(self, file_path: str) -> ScanResult:
        started = time.monotonic()
        path = Path(file_path)
        findings = []

        try:
            size = path.stat().st_size
            header = self._read_header(path)
            findings += self._check_executable(header)
            findings += self._check_eicar(path)

            if header.startswith(b"%PDF"):
                findings += self._check_pdf(path)
            elif header.startswith(b"PK\x03\x04"):
                findings += self._check_ooxml(path)
        except OSError as exc:
            # Cannot read the file, so cannot clear it. Raised rather than
            # returned: an unreadable file must never come back CLEAN.
            from .exceptions import ScannerUnavailable

            raise ScannerUnavailable(
                f"Could not read the file to scan it: {exc}"
            ) from exc

        verdict = Verdict.CLEAN
        if any(f.severity == "critical" for f in findings):
            verdict = Verdict.INFECTED
        elif findings:
            verdict = Verdict.SUSPICIOUS

        return ScanResult(
            verdict=verdict,
            findings=tuple(findings),
            scanner_name=self.name,
            scanner_version=self.version,
            ruleset_version=self.ruleset_version,
            duration_ms=int((time.monotonic() - started) * 1000),
            bytes_scanned=size,
            metadata={
                "rules_applied": [
                    "executable_header",
                    "eicar",
                    "pdf_active_content",
                    "ooxml_macro",
                ],
                "is_antivirus": False,
            },
        )

    @staticmethod
    def _read_header(path: Path, length: int = 1024) -> bytes:
        with path.open("rb") as handle:
            return handle.read(length)

    def _check_executable(self, header: bytes) -> list:
        for signature, description in EXECUTABLE_SIGNATURES:
            if header.startswith(signature):
                return [
                    Finding(
                        rule="executable_header",
                        severity="critical",
                        detail=(
                            f"File begins with a {description} header. A "
                            f"document upload is never an executable."
                        ),
                        location="offset 0",
                    )
                ]
        return []

    def _check_eicar(self, path: Path) -> list:
        for offset, chunk in self._chunks(path):
            index = chunk.find(EICAR)
            if index != -1:
                return [
                    Finding(
                        rule="eicar",
                        severity="critical",
                        detail="EICAR antivirus test signature present.",
                        location=f"offset {offset + index}",
                    )
                ]
        return []

    def _check_pdf(self, path: Path) -> list:
        findings = []
        seen = set()

        for offset, chunk in self._chunks(path):
            for pattern, description in PDF_ACTIVE_CONTENT:
                if description in seen:
                    continue
                match = pattern.search(chunk)
                if match:
                    seen.add(description)
                    # Active content in a PDF is a capability, not proof of
                    # intent - plenty of legitimate forms use it. Flagged
                    # for a human, not treated as infection.
                    findings.append(
                        Finding(
                            rule="pdf_active_content",
                            severity="warning",
                            detail=f"{description} found in the PDF.",
                            location=f"offset {offset + match.start()}",
                        )
                    )
        return findings

    def _check_ooxml(self, path: Path) -> list:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            # Not a readable archive. That is the ingestion stage's problem
            # to report, not a malware finding.
            return []

        findings = []
        for part in OOXML_MACRO_PARTS:
            if part in names:
                findings.append(
                    Finding(
                        rule="ooxml_macro",
                        severity="warning",
                        detail=(
                            f"Office file contains {part}, i.e. an embedded "
                            f"VBA macro project."
                        ),
                        location=part,
                    )
                )

        external = [n for n in names if n.endswith(".bin") and "oleObject" in n]
        if external:
            findings.append(
                Finding(
                    rule="ooxml_ole_object",
                    severity="warning",
                    detail=f"Office file embeds {len(external)} OLE object(s).",
                    location=", ".join(sorted(external)[:3]),
                )
            )

        return findings

    @staticmethod
    def _chunks(path: Path):
        """Yield (offset, bytes) with overlap, so a signature spanning a
        chunk boundary is still found."""
        with path.open("rb") as handle:
            offset = 0
            tail = b""
            while True:
                block = handle.read(CHUNK)
                if not block:
                    break
                buffer = tail + block
                yield offset - len(tail), buffer
                tail = buffer[-OVERLAP:]
                offset += len(block)

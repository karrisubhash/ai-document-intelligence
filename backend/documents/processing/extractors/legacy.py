"""Legacy Office files: .doc, .xls and .ppt, read through LibreOffice.

The binary formats Office wrote until 2007 are still what a great many
institutional archives hold, and no small library reads all three. LibreOffice
does, and reads them faithfully - tables, merged cells, sheets, slides, notes.
So a legacy file is CONVERTED to its modern form (.docx, .xlsx, .pptx) and the
converted file goes through exactly the extractor a modern file does: the same
tables, the same spans, the same picture and chart handling, nothing
re-implemented here.

The conversion is a subprocess with its own throwaway profile directory (two
LibreOffice processes sharing one profile lock each other out), a time limit,
and its working files in a temporary directory that is removed afterwards.
Nothing the file contains is executed: a headless conversion does not run
macros.

When LibreOffice is not installed the file fails with a message that says what
to do - save it in the modern format and upload that - rather than a generic
error. The container image installs it by default (see the Dockerfile's
LEGACY_OFFICE build argument).
"""

import contextlib
import os
import shutil
import subprocess
import tempfile

from django.conf import settings

from documents.processing.context import FileType
from documents.processing.errors import ErrorCode, StageError

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# What each legacy type is converted to.
TARGETS = {
    FileType.DOC: "docx",
    FileType.XLS: "xlsx",
    FileType.PPT: "pptx",
}
# The extension a legacy file is read as, when the bytes are an OLE container.
EXTENSIONS = {".doc": FileType.DOC, ".xls": FileType.XLS, ".ppt": FileType.PPT}

# A conversion that takes longer is a file LibreOffice cannot read.
DEFAULT_TIMEOUT = 150


def executable() -> str | None:
    configured = getattr(settings, "LIBREOFFICE_PATH", "") or ""
    if configured:
        return configured if os.path.exists(configured) else None
    return shutil.which("soffice") or shutil.which("libreoffice")


def available() -> bool:
    return executable() is not None


def _command(binary: str, profile: str, target: str, outdir: str, source: str) -> list:
    return [
        binary,
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--nofirststartwizard",
        "--convert-to",
        target,
        "--outdir",
        outdir,
        source,
    ]


@contextlib.contextmanager
def converted(file_path: str, kind: str):
    """The path of `file_path` converted to its modern form; the file is
    removed when the block ends."""
    binary = executable()
    target = TARGETS[kind]
    if binary is None:
        raise StageError(
            f"This is an old-format Office file (.{target[:-1]}) and the server "
            f"has no converter installed for it. Save it as .{target} in "
            "Office and upload that.",
            code=ErrorCode.UNSUPPORTED_FILE_TYPE,
        )

    timeout = int(getattr(settings, "LEGACY_CONVERT_TIMEOUT", DEFAULT_TIMEOUT))
    work = tempfile.mkdtemp(prefix="adi-legacy-")
    try:
        # LibreOffice picks its input filter partly from the name, so the copy
        # carries the legacy extension whatever the stored file is called.
        legacy_extension = "." + {"docx": "doc", "xlsx": "xls", "pptx": "ppt"}[target]
        source = os.path.join(work, "input" + legacy_extension)
        shutil.copyfile(file_path, source)
        outdir = os.path.join(work, "out")
        os.mkdir(outdir)
        profile = os.path.join(work, "profile")

        try:
            result = subprocess.run(
                _command(binary, profile.replace("\\", "/"), target, outdir, source),
                capture_output=True,
                timeout=timeout,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": work,
                    "LANG": "C.UTF-8",
                },
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise StageError(
                f"Converting the old-format file took longer than {timeout} "
                "seconds and was stopped.",
                code=ErrorCode.EXTRACTION_FAILED,
            ) from exc
        except OSError as exc:
            raise StageError(
                f"The converter could not be started: {exc}",
                code=ErrorCode.EXTRACTION_FAILED,
            ) from exc

        produced = os.path.join(outdir, f"input.{target}")
        if result.returncode != 0 or not os.path.exists(produced):
            detail = (
                (result.stderr or result.stdout or b"")
                .decode("utf-8", errors="replace")
                .strip()[:300]
            )
            raise StageError(
                "The old-format file could not be converted"
                + (f": {detail}" if detail else "."),
                code=ErrorCode.CORRUPT_DOCUMENT,
            )
        yield produced
    finally:
        shutil.rmtree(work, ignore_errors=True)

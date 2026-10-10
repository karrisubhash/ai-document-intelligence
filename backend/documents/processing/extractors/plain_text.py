from documents.processing.errors import ErrorCode, StageError


def read_text(file_path: str) -> str:
    try:
        # utf-8-sig, not utf-8: a byte-order mark is an encoding signature,
        # not text. Read as utf-8 it survived as U+FEFF at the start of page
        # 1, and a line-anchored field pattern no longer matched line one.
        with open(file_path, encoding="utf-8-sig", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise StageError(
            f"Could not read text file: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

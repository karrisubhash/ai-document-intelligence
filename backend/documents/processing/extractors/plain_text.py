from documents.processing.errors import ErrorCode, StageError


def read_text(file_path: str) -> str:
    try:
        with open(file_path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise StageError(
            f"Could not read text file: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc

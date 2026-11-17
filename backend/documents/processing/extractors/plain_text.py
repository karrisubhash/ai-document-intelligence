import codecs

from documents.processing.errors import ErrorCode, StageError

# UTF-32 first: its little-endian mark (FF FE 00 00) begins with UTF-16's.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def detect_encoding(sample: bytes) -> str:
    """The encoding of a text file, from its first bytes.

    A byte-order mark says so outright. Without one, a file that decodes as
    UTF-8 is UTF-8, and one that does not is what Windows wrote: Notepad's
    "ANSI" and Excel's "CSV" are cp1252, in which every byte but five decodes,
    so a name with an accent or a rupee sign stays itself. Read as UTF-8 with
    errors replaced - which this module did - each of those became a
    replacement character, and a UTF-16 file (Notepad's "Unicode") became
    text with a NUL between every letter.
    """
    for mark, name in _BOMS:
        if sample.startswith(mark):
            return name

    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        # final=False: the sample may end half way through a multi-byte
        # character, which is not an encoding error.
        decoder.decode(sample, final=False)
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def decode(raw: bytes) -> str:
    """Bytes of a text file as text, whatever Windows or an editor wrote."""
    encoding = detect_encoding(raw[:65536])
    text = raw.decode(encoding, errors="replace")
    # A byte-order mark is an encoding signature, not text: left in, it was
    # U+FEFF at the start of page 1, and a line-anchored field pattern no
    # longer matched line one.
    return text.lstrip("﻿")


def read_text(file_path: str) -> str:
    try:
        with open(file_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise StageError(
            f"Could not read text file: {exc}",
            code=ErrorCode.EXTRACTION_FAILED,
        ) from exc
    return decode(raw)

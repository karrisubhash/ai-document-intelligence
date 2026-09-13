"""Text normalization for the OCR-text classification baseline.

Stdlib-only and *parameterised* rather than hard-coded, on purpose. The
exact parameters used at training time are written into the model
artifact, and the inference adapter on the Django side reads them back
out and applies the same transformation. Training and serving therefore
cannot drift apart without the artifact itself saying so — which is the
only reason it is safe for the two sides to share no code.

What is deliberately *not* done here matters as much as what is. Numbers
and punctuation survive: invoice numbers, dates, amounts, course codes
and document IDs are signal for this task, not noise.
"""

import unicodedata
from pathlib import Path

# The four Unicode normal forms. Anything else is a typo or a parameter
# from a newer artifact this code does not implement - either way it must
# fail rather than silently normalize differently than training did.
UNICODE_FORMS = ("NFC", "NFD", "NFKC", "NFKD")

PARAMETER_NAMES = frozenset(
    {"unicode_normalization", "lowercase", "collapse_whitespace"}
)

DEFAULT_PARAMETERS = {
    "unicode_normalization": "NFKC",
    "lowercase": True,
    "collapse_whitespace": True,
}


def normalize(
    text: str,
    *,
    unicode_normalization: str | None = "NFKC",
    lowercase: bool = True,
    collapse_whitespace: bool = True,
) -> str:
    """Apply the normalization pipeline in a fixed, documented order.

    Order is part of the contract: normalize, then case-fold, then
    collapse whitespace. Reordering these changes the output for some
    inputs, so it is pinned here and tested rather than left implicit.
    """
    if unicode_normalization:
        if unicode_normalization not in UNICODE_FORMS:
            raise ValueError(
                f"unknown unicode normalization {unicode_normalization!r}; "
                f"expected one of {', '.join(UNICODE_FORMS)}"
            )
        text = unicodedata.normalize(unicode_normalization, text)

    if lowercase:
        text = text.lower()

    if collapse_whitespace:
        # Also strips, since str.split() drops leading/trailing runs.
        text = " ".join(text.split())

    return text


def apply(text: str, parameters: dict) -> str:
    """Normalize using a parameter dict, rejecting anything unrecognised.

    This is the function the inference side calls with the parameters it
    read from a model artifact. An unknown key means the artifact was
    produced by a preprocessing pipeline this code does not implement,
    and quietly ignoring it would mean serving inputs shaped differently
    from the ones the model was fitted on.
    """
    unknown = set(parameters) - PARAMETER_NAMES
    if unknown:
        raise ValueError(
            f"unsupported preprocessing parameters: {sorted(unknown)}; "
            f"this code implements {sorted(PARAMETER_NAMES)}"
        )

    return normalize(text, **{**DEFAULT_PARAMETERS, **parameters})


def read_document(path) -> str:
    """Read one OCR text file.

    A missing file is an error: the integrity gate runs over these same
    manifests before training and is supposed to have caught it. An
    *empty* file is not an error - 2.42% of QS-OCR documents have no OCR
    text at all, concentrated in the low-text classes, and dropping them
    would shift the class distribution underneath the experiment.
    """
    # Manifests are written with the host's separator; accept either so a
    # manifest built on Windows still reads on POSIX.
    resolved = Path(str(path).replace("\\", "/"))

    if not resolved.is_file():
        raise FileNotFoundError(
            f"manifest points at a missing document: {resolved}. "
            f"Re-run the dataset adapter and the integrity check."
        )

    return resolved.read_text(encoding="utf-8", errors="replace")


def load_texts(samples, parameters: dict) -> list[str]:
    """Read and normalize every sample's document, preserving order."""
    return [apply(read_document(s.input_path), parameters) for s in samples]

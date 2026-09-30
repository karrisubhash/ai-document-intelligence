"""A deterministic, offline embedding baseline.

This is NOT a semantic embedder and does not pretend to be. It hashes word
and character n-grams into a fixed-width vector, so two texts are close when
they share vocabulary, not when they share meaning. "car" and "automobile"
are orthogonal here.

It exists because the alternative was shipping no vector path at all: no LLM
provider is wired up in this project, no API keys exist, and downloading a
sentence-transformer at runtime is not something a worker should do on its
first document. What this buys is the whole indexing and retrieval
mechanism - chunking, vector storage, the pgvector index, fusion with the
lexical side, the permission filter - built and measurable against a
baseline that costs nothing and never varies.

`semantic = False` travels with every batch and is recorded on every stored
embedding, so a search result can say plainly which kind of similarity
produced it. Swapping in a real model is a settings change plus the
re-indexing that section 11.4 already requires when the embedding model
changes.
"""

import hashlib
import math
import re
import unicodedata

# Must equal EMBEDDING_DIMENSIONS in documents.models.chunk - the width the
# pgvector column and its HNSW index are built for. Stated here as a plain
# int rather than imported, so this module keeps working without the Django
# app registry loaded (the trainers in ai/ import it). The two are not left
# to drift: chunking.service.get_provider() compares them on every load and
# refuses a provider whose width does not match the column.
DIMENSIONS = 384

WORD = re.compile(r"\w+", re.UNICODE)

# Character n-grams give partial credit for morphology and OCR noise that
# whole-word hashing misses entirely - "regulation" vs "regulations", or a
# scanned "reguIation" with a capital i.
CHAR_NGRAM = 4


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def _bucket(token: str, dimensions: int) -> tuple:
    """A stable (index, sign) for a token.

    blake2b rather than hash() because Python randomises str hashing per
    process: the same document must embed identically in the worker that
    indexes it and the API process that searches it.
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    # Signed hashing: half the tokens subtract. This keeps unrelated
    # collisions from all pushing in the same direction.
    return value % dimensions, 1.0 if (value >> 63) & 1 else -1.0


def embed_text(text: str, dimensions: int = DIMENSIONS) -> list:
    vector = [0.0] * dimensions
    normalized = _normalize(text)

    words = WORD.findall(normalized)
    for word in words:
        index, sign = _bucket(f"w:{word}", dimensions)
        vector[index] += sign

    for word in words:
        # Boundary markers so a prefix and a suffix hash differently.
        padded = f"^{word}$"
        for start in range(max(1, len(padded) - CHAR_NGRAM + 1)):
            gram = padded[start : start + CHAR_NGRAM]
            index, sign = _bucket(f"c:{gram}", dimensions)
            # Weighted below whole words: n-grams are a similarity hint,
            # not the primary signal.
            vector[index] += sign * 0.3

    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        # An empty or symbol-only text. Returned as zeros rather than
        # normalised into a nonsense direction; callers skip these.
        return vector

    return [value / norm for value in vector]


class HashingEmbeddingProvider:
    """Lexical hashing embeddings. Deterministic, offline, not semantic."""

    name = "hashing-baseline"
    version = "0.1.0"
    dimensions = DIMENSIONS
    semantic = False

    def embed(self, texts: list):
        from .base import EmbeddingBatch

        vectors = tuple(embed_text(text, self.dimensions) for text in texts)

        return EmbeddingBatch(
            vectors=vectors,
            model_name=self.name,
            model_version=self.version,
            dimensions=self.dimensions,
            semantic=self.semantic,
            metadata={"char_ngram": CHAR_NGRAM},
        )

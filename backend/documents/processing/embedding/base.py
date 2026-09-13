from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EmbeddingBatch:
    """Vectors for a batch of texts, in the order they were given."""

    vectors: tuple
    model_name: str
    model_version: str
    dimensions: int
    # Whether these vectors carry meaning beyond word overlap. The baseline
    # provider is lexical, and a retrieval result that says otherwise would
    # misrepresent what the search actually did.
    semantic: bool = False
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """The whole contract between indexing/search and any embedder.

    Implementations may be a hashing baseline, a local sentence-transformer
    or a remote API - the pipeline is not allowed to care. The same provider
    must embed documents at index time and queries at search time, or the
    two live in different spaces and every score is meaningless.
    """

    name: str
    version: str
    dimensions: int
    semantic: bool

    def embed(self, texts: list) -> EmbeddingBatch: ...

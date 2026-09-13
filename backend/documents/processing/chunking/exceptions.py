class ChunkingError(Exception):
    """A chunk set could not be produced or is internally inconsistent.

    Permanent: the same content chunked again by the same code yields the
    same problem, so retrying only burns worker capacity.
    """


class EmbeddingProviderNotConfigured(Exception):
    """No embedding provider is wired up.

    Not an error. Chunks are still written and still lexically searchable;
    only the vector half of hybrid retrieval is absent, and it can be
    backfilled once a provider is configured.
    """


class EmbeddingProviderUnavailable(Exception):
    """A configured provider could not be loaded, or failed mid-batch.

    Distinct from NotConfigured: something is wired up but failing, which
    may be worth retrying - a remote provider especially.
    """

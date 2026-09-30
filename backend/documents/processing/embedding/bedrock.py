"""Amazon Titan Text Embeddings V2, behind the EmbeddingProvider protocol.

This is the first embedder in this project that actually encodes meaning.
The bundled `HashingEmbeddingProvider` hashes vocabulary into buckets, so
"car" and "automobile" are orthogonal and a question phrased differently
from the document retrieves nothing. That is survivable across five
documents, where the lexical half of hybrid retrieval carries the query on
its own. It is not survivable across twelve thousand, where the words a
reader chooses and the words a 1970s memo chose overlap far less often than
the meaning does.

`semantic = True` travels with every batch and is stored on every row, so a
search result can still say which kind of similarity produced it - the same
honesty the baseline had, now with the other answer.

WIDTH. Titan V2 emits 1024 dimensions natively, and `EMBEDDING_DIMENSIONS`
was widened from 384 to match. It can also emit 512 or 256 (99% and 97% of
the retrieval accuracy, at half and a quarter of the storage); either is a
defensible choice and either needs the column, this provider and a full
re-index changed together, never separately.

ONE TEXT PER CALL. Titan's InvokeModel body takes `inputText`, singular -
there is no batch form. The caller hands us up to `EMBED_BATCH_SIZE` texts
at a time and expects one vector each, in order, so the batch is fanned out
across a small thread pool and reassembled by index. Without the pool,
indexing the corpus is one HTTP round trip per chunk, serially, which turns
a twenty-minute re-index into several hours. With it, the bound is Bedrock's
throttle rather than the network's latency.

(Cohere's Bedrock embedder does take a real batch - up to 96 texts per call -
and would be fewer round trips. It is not the default because it needs an
`input_type` that differs between indexing a passage and embedding a query,
and this protocol has one `embed()` for both. Titan is symmetric and
sidesteps that; a Cohere provider would need to carry the distinction.)

THROTTLING IS EXPECTED, NOT EXCEPTIONAL. A bulk re-index will hit Bedrock's
per-account rate limit, and the right response is to slow down rather than
to fail the document. botocore's `adaptive` retry mode does exactly that:
it retries with backoff *and* maintains a client-side rate limiter that
learns the ceiling. Failures that survive it propagate, and the caller
turns them into `EmbeddingProviderUnavailable`, which is retryable at the
task level - the chunks are already stored by then, so nothing is lost.
"""

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from .base import EmbeddingBatch

logger = logging.getLogger("adi.embedding")

MODEL_ID = "amazon.titan-embed-text-v2:0"

# Titan V2 accepts 8,192 tokens / 50,000 characters. Chunks are ~1,000
# characters, so this is a guard against a pathological chunk rather than a
# limit anyone should reach. Truncation is logged, because silently
# embedding the first half of a passage and citing the whole of it would
# make the vector disagree with the text it claims to represent.
MAX_INPUT_CHARS = 50_000

# Concurrent InvokeModel calls per batch. Deliberately modest: the point is
# to stop paying full network latency per chunk, not to race the throttle.
# Adaptive retries handle the ceiling; a larger pool would mostly generate
# retries and make the effective rate worse.
DEFAULT_CONCURRENCY = 8


class BedrockEmbeddingUnavailable(RuntimeError):
    """boto3 is missing, or there is nothing to authenticate with."""


class BedrockTitanEmbeddingProvider:
    """Semantic embeddings from Amazon Titan Text Embeddings V2."""

    name = "bedrock-titan-embed-text-v2"
    semantic = True

    def __init__(self):
        from documents.models.chunk import EMBEDDING_DIMENSIONS

        self.model_id = getattr(settings, "EMBEDDING_BEDROCK_MODEL", "") or MODEL_ID
        # The version IS the model id. An embedding is only comparable to
        # another produced by the same model, and the uniqueness constraint
        # on (chunk, model_name, model_version) is what keeps two models'
        # vectors from being mistaken for each other during a migration.
        self.version = self.model_id

        # Asked for explicitly rather than inferred from the response, so a
        # mismatch with the column is caught at load time by
        # chunking.service.get_provider() rather than at the first INSERT.
        self.dimensions = int(
            getattr(settings, "EMBEDDING_DIMENSIONS", 0) or EMBEDDING_DIMENSIONS
        )

        self.concurrency = int(
            getattr(settings, "EMBEDDING_BEDROCK_CONCURRENCY", 0) or DEFAULT_CONCURRENCY
        )

        self._client = None
        self._lock = threading.Lock()

    # --- client ----------------------------------------------------------

    def _region(self) -> str:
        """The Bedrock region, or a refusal.

        Required rather than defaulted, for the reason the answerer gives:
        a silent us-east-1 sends traffic to a region the account may have
        no model access in, and the resulting error names the model rather
        than the region, which sends whoever debugs it to the wrong place.
        """
        region = (
            getattr(settings, "AWS_BEDROCK_REGION", "")
            or os.environ.get("AWS_REGION", "")
            or os.environ.get("AWS_DEFAULT_REGION", "")
        )
        if not region:
            raise BedrockEmbeddingUnavailable(
                "Bedrock embeddings need a region. Set AWS_BEDROCK_REGION "
                "(or AWS_REGION) to a region where you have been granted "
                f"model access to {self.model_id}."
            )
        return region

    def _get_client(self):
        """One boto3 client, created on first use and shared by the pool.

        boto3 clients are thread-safe for calls but not for construction,
        hence the lock. Built lazily so that merely importing this module -
        which `manage.py check` and the test suite both do - needs neither
        boto3 nor credentials.
        """
        if self._client is not None:
            return self._client

        with self._lock:
            if self._client is not None:
                return self._client

            region = self._region()

            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise BedrockEmbeddingUnavailable(
                    "boto3 is not installed. "
                    "pip install -r backend/requirements-llm.txt"
                ) from exc

            config = Config(
                region_name=region,
                retries={
                    "max_attempts": int(
                        getattr(settings, "EMBEDDING_BEDROCK_RETRIES", 0) or 6
                    ),
                    # Retries with backoff AND a client-side rate limiter
                    # that learns the account's ceiling. "standard" would
                    # retry the throttle without ever slowing down, which
                    # on a bulk re-index means retrying into the same wall.
                    "mode": "adaptive",
                },
                read_timeout=int(
                    getattr(settings, "EMBEDDING_BEDROCK_TIMEOUT", 0) or 30
                ),
                connect_timeout=10,
            )

            self._client = boto3.client("bedrock-runtime", config=config)
            return self._client

    # --- embedding -------------------------------------------------------

    def embed(self, texts: list) -> EmbeddingBatch:
        vectors = [None] * len(texts)

        # Whitespace-only chunks never reach Bedrock. Titan rejects an
        # empty inputText outright, and a zero vector is the honest
        # representation anyway - the hashing baseline returns one for the
        # same case, and search skips them.
        pending = []
        for index, text in enumerate(texts):
            prepared = (text or "").strip()
            if not prepared:
                vectors[index] = [0.0] * self.dimensions
                continue
            if len(prepared) > MAX_INPUT_CHARS:
                logger.warning(
                    "embedding.truncated",
                    extra={
                        "characters": len(prepared),
                        "limit": MAX_INPUT_CHARS,
                        "model": self.model_id,
                    },
                )
                prepared = prepared[:MAX_INPUT_CHARS]
            pending.append((index, prepared))

        if pending:
            client = self._get_client()
            workers = max(1, min(self.concurrency, len(pending)))

            with ThreadPoolExecutor(max_workers=workers) as pool:
                # Results are placed by their original index rather than
                # collected in completion order. The protocol promises one
                # vector per text *in the order given*, and a thread pool
                # does not finish in the order it started; getting this
                # wrong would attach every vector to the wrong chunk and
                # produce a search that is subtly, silently wrong.
                for index, vector in pool.map(
                    lambda item: (item[0], self._embed_one(client, item[1])),
                    pending,
                ):
                    vectors[index] = vector

        return EmbeddingBatch(
            vectors=tuple(vectors),
            model_name=self.name,
            model_version=self.version,
            dimensions=self.dimensions,
            semantic=self.semantic,
            metadata={"model_id": self.model_id, "normalized": True},
        )

    def _embed_one(self, client, text: str) -> list:
        response = client.invoke_model(
            modelId=self.model_id,
            accept="application/json",
            contentType="application/json",
            body=json.dumps(
                {
                    "inputText": text,
                    "dimensions": self.dimensions,
                    # Cosine distance is what the HNSW index is built for
                    # (vector_cosine_ops), and normalised vectors make it
                    # equivalent to a dot product. Asking Titan to
                    # normalise is cheaper and more accurate than doing it
                    # here in Python.
                    "normalize": True,
                }
            ),
        )

        payload = json.loads(response["body"].read())
        vector = payload.get("embedding")

        if not isinstance(vector, list) or len(vector) != self.dimensions:
            # Caught here rather than at the database, where the failure
            # would be a pgvector width error several frames away from the
            # call that caused it.
            raise BedrockEmbeddingUnavailable(
                f"{self.model_id} returned "
                f"{len(vector) if isinstance(vector, list) else type(vector).__name__} "
                f"values, expected {self.dimensions}."
            )

        return vector

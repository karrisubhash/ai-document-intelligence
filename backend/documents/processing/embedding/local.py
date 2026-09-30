"""Sentence-transformer embeddings, computed in this process.

This is the third answer to "what produces the vectors", and it exists
because the other two each ask for something a deployment may not have.
The bundled `HashingEmbeddingProvider` asks for nothing at all and pays
for it by being lexical: it hashes vocabulary into buckets, so "car" and
"automobile" are orthogonal and a question phrased unlike the document it
should find retrieves nothing from the vector half.
`BedrockTitanEmbeddingProvider` encodes meaning, and asks for an AWS
account, granted model access, a network round trip per chunk and -
decisively - a column 1024 wide, which is a migration and a full re-index
rather than a settings change.

A small sentence-transformer sits between them: real semantic embeddings,
no account, no network at query time, and 384 dimensions, which is the
width `EMBEDDING_DIMENSIONS` and the HNSW index are already built for.
Turning it on is a settings change plus the controlled re-index that
section 11.4 requires whenever the embedding model changes - no migration.
What it costs is local CPU and roughly 134 MB of weights resident in every
worker process.

THE MODEL. `BAAI/bge-small-en-v1.5` by default: 33M parameters, a
512-token window, 384 dimensions, and a graph that already L2-normalises
its own output. On CPU it embeds a chunk in single-digit milliseconds,
which is what keeps a twelve-thousand-document corpus a matter of minutes
instead of hours.

NO QUERY INSTRUCTION, ON PURPOSE. bge publishes a query-side instruction -
"Represent this sentence for searching relevant passages: " - and no
passage-side one. This protocol has exactly one `embed()`, called by
chunking.service for passages and by search.service for queries, and
nothing in the call distinguishes them. Prefixing everything would put the
instruction on passages, where the model card says it does not belong;
prefixing nothing costs, in that card's own words, only "a slight
degradation in retrieval performance". So nothing is prefixed, on either
side, and queries and passages stay in one vector space by construction
rather than by luck.

That property is why this model is the default and `intfloat/e5-small-v2`
is not. e5 needs "query: " and "passage: " on their respective sides and
its card promises "a performance degradation" without them, so there is no
graceful way to serve it through a single `embed()`. Adding the
instruction later means a second method on the protocol, the search call
site switched over to it, and a full re-index - deliberately not a flag
here, because a flag that put a query instruction on stored passages would
surface as mediocre recall rather than as an error. Note also that
sentence-transformers' own `encode_query()` would not apply the
instruction for us: it only consults a `prompts` dictionary, and none of
the small models in this family ships one.

LOADED ONCE, LAZILY. Importing this module must not import torch, must not
touch the network and must not pay a multi-second model load: `manage.py
check` and the test suite both import it, and neither embeds anything. The
model is therefore built on the first `embed()` and cached on the instance
behind a lock. A forked gunicorn or Celery worker gets its own copy of the
weights, which is the memory price of running the model in-process rather
than behind an API.

WHERE THE WEIGHTS COME FROM. The first construction downloads them from
the Hugging Face hub into SENTENCE_TRANSFORMERS_HOME (or HF_HOME). In a
container with no baked cache, that download lands inside the first web
request or the first Celery task, and on a read-only filesystem it becomes
a hard failure at exactly that moment. Bake the model into the image or
mount a cache volume; HF_HUB_OFFLINE=1 then turns a missing cache into an
immediate and obvious failure instead of a surprise 134 MB fetch.
"""

import logging
import threading
import time

from django.conf import settings

from .base import EmbeddingBatch

logger = logging.getLogger("adi.embedding")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# How many texts go through the model at once. This is a memory/throughput
# knob inside one process, not a network batch - the caller's
# EMBED_BATCH_SIZE still decides how many chunks arrive per embed() call.
DEFAULT_BATCH_SIZE = 32

# CPU unless told otherwise. Guessing is the worse default: with device
# unset, sentence-transformers picks CUDA whenever a GPU is visible, so on
# a shared box the web worker quietly takes GPU memory from whatever else
# is using it - and a corpus this size does not need it.
DEFAULT_DEVICE = "cpu"

# Roughly where a 512-token window ends, at ~4 characters per token. Past
# it the tokenizer truncates SILENTLY - no error, no shorter output, just a
# vector for the first part of a passage that the citation then presents as
# the whole of it. The chunker emits ~1,000 characters, so this is a guard
# against a pathological chunk rather than a limit anyone should reach.
MAX_INPUT_CHARS = 2_000

# Model families whose cards make the query/passage prefix mandatory
# rather than optional. This provider applies no prefix at all (see the
# module docstring), which is a mild loss for bge and a real one for these,
# so pointing EMBEDDING_LOCAL_MODEL at one is worth saying out loud: the
# symptom otherwise is recall that is merely disappointing, and nothing
# connects that back to the setting that caused it.
INSTRUCTION_REQUIRED_MODELS = ("intfloat/e5", "intfloat/multilingual-e5")


class LocalEmbeddingUnavailable(RuntimeError):
    """sentence-transformers is missing, or the model will not load."""


class LocalEmbeddingProvider:
    """Semantic embeddings from a sentence-transformer, in this process."""

    name = "sentence-transformers"
    semantic = True

    def __init__(self):
        from documents.models.chunk import EMBEDDING_DIMENSIONS

        self.model_id = getattr(settings, "EMBEDDING_LOCAL_MODEL", "") or DEFAULT_MODEL

        # The version IS the model id. A vector is only comparable to
        # another produced by the same model, and the uniqueness constraint
        # on (chunk, model_name, model_version) is what stops two models'
        # vectors being mistaken for each other during a migration. Every
        # id in this family fits model_version's 50 characters; a longer
        # one needs that column widened rather than truncating here, where
        # two different models could collide on one version string.
        self.version = self.model_id

        # Declared rather than discovered, so a mismatch with the column is
        # caught by chunking.service.get_provider() at load time. It is
        # checked again against the loaded model below, because until then
        # this is a claim about a model nobody has opened.
        self.dimensions = int(
            getattr(settings, "EMBEDDING_DIMENSIONS", 0) or EMBEDDING_DIMENSIONS
        )

        # Floored at 1. encode() steps through the batch with
        # range(0, len(texts), batch_size), so a negative value iterates
        # zero times and returns zero vectors - which surfaces here as
        # "returned 0 vectors for 64 texts" on every single embed, naming
        # the model rather than the mis-set environment variable that
        # actually caused it. Zero is already absorbed by the `or` below;
        # this covers the rest.
        self.batch_size = max(
            1,
            int(getattr(settings, "EMBEDDING_LOCAL_BATCH_SIZE", 0) or 0)
            or DEFAULT_BATCH_SIZE,
        )
        self.device = getattr(settings, "EMBEDDING_LOCAL_DEVICE", "") or DEFAULT_DEVICE
        # 0 means leave torch's own choice alone. See _get_model().
        self.threads = int(getattr(settings, "EMBEDDING_LOCAL_THREADS", 0) or 0)

        self._model = None
        self._lock = threading.Lock()

    # --- model -----------------------------------------------------------

    def _get_model(self):
        """The loaded model, built once per process on first use.

        Double-checked under a lock because gunicorn's threads and Celery's
        pool can both reach the first embed() at the same moment, and
        loading 134 MB of weights twice concurrently is a memory spike big
        enough to get a container killed.
        """
        if self._model is not None:
            return self._model

        with self._lock:
            if self._model is not None:
                return self._model

            try:
                # torch is imported here rather than under `if self.threads`
                # below, because sentence_transformers imports it anyway and
                # a broken install does not fail as an ImportError: a CUDA
                # wheel on a machine with no driver raises OSError from the
                # DLL load, and that escaped this handler raw. Catching the
                # import outright means every "the library will not load"
                # arrives as the one exception the callers know about.
                import torch
                from sentence_transformers import SentenceTransformer
            except Exception as exc:
                raise LocalEmbeddingUnavailable(
                    f"sentence-transformers will not import ({exc}). Install "
                    "the CPU build of torch FIRST - otherwise pip resolves "
                    "the CUDA wheel and pulls in several GB of nvidia "
                    "packages this provider never uses. The pinned CPU "
                    "wheels and the index they come from are both in:\n"
                    "  pip install -r backend/requirements-local.txt"
                ) from exc

            if self.threads:
                # torch reads the host's core count, not the cgroup CPU
                # limit, so in a constrained container it starts far more
                # threads than it has CPU for and embedding gets slower
                # under load rather than faster. Set this to the container's
                # actual allocation.
                torch.set_num_threads(self.threads)

            if self.model_id.startswith(INSTRUCTION_REQUIRED_MODELS):
                logger.warning(
                    "embedding.instruction_not_applied",
                    extra={
                        "model": self.model_id,
                        "detail": (
                            "this model's card requires query/passage "
                            "prefixes and this provider applies none, so "
                            "retrieval will sit below the model's published "
                            "numbers"
                        ),
                    },
                )

            started = time.monotonic()

            try:
                model = SentenceTransformer(self.model_id, device=self.device)
            except Exception as exc:
                # Nearly always a download that could not happen: no
                # network, no writable cache, or HF_HUB_OFFLINE set over an
                # empty one. Re-raised with the model id and device in the
                # message, because the underlying errors name a cache path
                # and leave the reader to guess which model wanted it.
                raise LocalEmbeddingUnavailable(
                    f"Could not load {self.model_id} on {self.device}: {exc}"
                ) from exc

            # Renamed in sentence-transformers: get_sentence_embedding_dimension
            # is deprecated in favour of get_embedding_dimension and emits a
            # FutureWarning on every model load. Preferring the new name and
            # falling back keeps this working across both, rather than
            # pinning the library or carrying a warning into the logs of
            # every worker that ever embeds anything.
            measure = getattr(
                model,
                "get_embedding_dimension",
                getattr(model, "get_sentence_embedding_dimension", None),
            )
            width = measure() if callable(measure) else None
            if width is not None and width != self.dimensions:
                # Caught here rather than at the first INSERT, where the
                # failure is a pgvector width error several frames from the
                # setting that caused it, and half the corpus is already in.
                raise LocalEmbeddingUnavailable(
                    f"{self.model_id} emits {width}-dimensional vectors but "
                    f"the column is {self.dimensions}. Moving to a model of a "
                    f"different width requires a migration and a full "
                    f"re-index, not just EMBEDDING_LOCAL_MODEL."
                )

            default_prompt = getattr(model, "default_prompt_name", None)
            if default_prompt:
                # A model carrying a default prompt applies it to every text
                # this provider encodes, passages included - the exact
                # asymmetry the module docstring refuses to introduce.
                # Recorded rather than raised: it is a quality question, not
                # a correctness one, and both sides stay at least consistent
                # with each other.
                logger.warning(
                    "embedding.default_prompt_applied",
                    extra={"model": self.model_id, "prompt": default_prompt},
                )

            logger.info(
                "embedding.model_loaded",
                extra={
                    "model": self.model_id,
                    "device": self.device,
                    "dimensions": self.dimensions,
                    "seconds": round(time.monotonic() - started, 2),
                },
            )

            self._model = model
            return self._model

    # --- embedding -------------------------------------------------------

    def embed(self, texts: list) -> EmbeddingBatch:
        vectors = [None] * len(texts)

        # Whitespace-only chunks never reach the model. It would happily
        # embed an empty string, and the result is a perfectly valid vector
        # pointing somewhere arbitrary - which then matches every other
        # empty chunk and any near-empty query. Zeros are the honest
        # representation, they are what the hashing baseline returns for the
        # same case, and search skips them.
        pending_indexes = []
        pending_texts = []
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
            pending_indexes.append(index)
            pending_texts.append(prepared)

        if pending_texts:
            model = self._get_model()

            encoded = model.encode(
                pending_texts,
                batch_size=self.batch_size,
                # The HNSW index is built with vector_cosine_ops, and unit
                # vectors make that distance a dot product. This family
                # normalises inside the model graph already, so the flag is
                # currently idempotent - passed anyway, so the invariant
                # survives a swap to a model that does not.
                normalize_embeddings=True,
                # Defaults to None, which resolves to "on if logging is at
                # INFO or DEBUG" - that is, tqdm bars sprayed through the
                # gunicorn and Celery logs on every single query embed.
                show_progress_bar=False,
                convert_to_numpy=True,
            )

            # numpy float32 scalars are not what psycopg and pgvector want;
            # .tolist() converts the whole array to native Python floats in
            # one pass.
            rows = encoded.tolist()

            if len(rows) != len(pending_texts):
                raise LocalEmbeddingUnavailable(
                    f"{self.model_id} returned {len(rows)} vectors for "
                    f"{len(pending_texts)} texts."
                )

            # Put back at their original positions. The protocol promises
            # one vector per text IN THE ORDER GIVEN, and the zero-vector
            # texts above were lifted out of the middle of that order;
            # getting this wrong attaches every vector to the wrong chunk
            # and produces a search that is subtly, silently incorrect.
            # strict=True: the length check above already raises, but a
            # future refactor that drops it would otherwise make zip()
            # truncate silently, which is exactly the wrong-vector-per-
            # chunk failure this comment warns about.
            for index, row in zip(pending_indexes, rows, strict=True):
                if len(row) != self.dimensions:
                    raise LocalEmbeddingUnavailable(
                        f"{self.model_id} returned {len(row)} values, "
                        f"expected {self.dimensions}."
                    )
                vectors[index] = row

        return EmbeddingBatch(
            vectors=tuple(vectors),
            model_name=self.name,
            model_version=self.version,
            dimensions=self.dimensions,
            semantic=self.semantic,
            metadata={
                "model_id": self.model_id,
                "device": self.device,
                "normalized": True,
                # Recorded per batch because it is the one retrieval-quality
                # decision here that cannot be read off the model id:
                # queries and passages are both embedded bare.
                "query_instruction": "none",
            },
        )

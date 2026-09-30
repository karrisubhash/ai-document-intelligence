"""A cross-encoder reranker running in this process, on CPU.

This is the local sibling of `bedrock_rerank.BedrockReranker` and the free
half of the same idea. Both replace the bundled `LexicalReranker`, which
scores term overlap and is therefore blind to the passage that answers a
question in words the question did not use. What this one asks for instead
of an AWS account, granted model access and a network round trip per query
is roughly 91 MB of weights resident in every worker process and a few
hundred milliseconds of CPU per question. Reranking only ever sees
`RAG_RETRIEVAL_DEPTH` candidates, which is what makes that affordable here
and would not make it affordable for embedding.

THE MODEL. `cross-encoder/ms-marco-MiniLM-L6-v2` by default: 22.7M
parameters, 91 MB of fp32 weights, a 512-token window covering the query
and the passage together. The BAAI rerankers are the obvious alternatives
and both lose on the axis that decides this one: `bge-reranker-base`
carries roughly 8x the compute-bearing parameters and `bge-reranker-v2-m3`
roughly 27x, which on CPU is the difference between a reranked question and
a timed-out one. Their extra capacity is multilingual, and this corpus is
English. The quality gap to the twice-as-slow L12 variant of this same
model is 0.01 NDCG@10, which is noise.

THE SCORE RANGE IS NOT THE LIBRARY'S DEFAULT, AND THAT IS THE FAILURE THIS
MODULE IS ARRANGED AROUND. sentence-transformers resolves a cross-encoder's
output activation from the model's own config, so the SAME `predict()` call
returns a different kind of number depending on which model is loaded. This
model's config.json pins `sbert_ce_default_activation_function` to
Identity, so it emits RAW LOGITS - unbounded, routinely negative, roughly
-10 to +10. The BAAI rerankers pin nothing and have a single label, so the
library applies a sigmoid and they emit (0, 1). Handing logits to a
relevance floor written for [0, 1] does not raise: a floor of 0.10 then
admits every passage with a positive logit and rejects every passage with a
negative one, which is a coin-flip cut through the middle of the
distribution that looks exactly like a working threshold. So the activation
is passed EXPLICITLY on every load rather than inherited, and the scores are
checked against [0, 1] again before they leave this module. Sigmoid is
strictly monotone, so none of that changes the ranking - only the scale the
floor is applied to.

WHAT THE SCORES ARE NOT. Sigmoid over logits spanning +/-10 saturates:
above a logit of 5 everything is >0.993 and below -5 everything is <0.007,
so the distribution is bimodal rather than spread. That makes it a good
absolute, query-independent gate - which is exactly what a relevance floor
needs - and a bad number to show a reader as a confidence. Per-query
min-max normalisation would give a pretty spread and destroy the gate,
because a candidate list in which every passage is irrelevant still
normalises its best passage to 1.0.

LONG PASSAGES LOSE THEIR TAIL, SILENTLY. The pair is truncated
`longest_first`, which drops tokens from whichever side is longer: the
query survives intact and the end of the passage goes, with no error and no
warning. A passage past the window is therefore judged on its opening. The
chunker emits roughly 1,000 characters, so this is a guard against a
pathological chunk rather than a limit anyone should reach - but it is
recorded per candidate when it happens, because "ranked low" and "ranked
low on the first half of its text" are different facts about a result.

LOADED ONCE, LAZILY, for the reason the local embedding provider gives:
importing this module must not import torch, must not touch the network and
must not pay a multi-second model load, since `manage.py check` and the test
suite both import it and neither reranks anything. The first `rerank()`
builds the model and caches it on the instance behind a lock. That first
construction also DOWNLOADS the weights from the Hugging Face hub, which in
a container with no baked cache happens inside the first search request -
bake the model into the image or mount a cache volume, and set
HF_HUB_OFFLINE=1 so a missing cache fails immediately and obviously rather
than fetching 91 MB underneath a waiting user.

FAILURE FALLS BACK RATHER THAN 500s, on the same principle as the answerer
and the Bedrock reranker: a missing package, an unwritable cache or an
out-of-memory kill degrades result ORDERING, which is worth far less than
taking search down. The degradation is recorded in every result's signals,
because a quietly degraded ranking that does not say so is worse than a
slow one.
"""

import logging
import math
import threading
import time

from django.conf import settings

from documents.processing.model_cache import load_cached_first

from .reranking import LexicalReranker, RerankedChunk

logger = logging.getLogger("adi.rerank")

# bge-reranker-base, not ms-marco-MiniLM. Both were measured on this
# project's own paraphrase cases, and MiniLM did not merely score worse -
# it ranked the WRONG passage first, which is the one outcome a reranker
# must not produce:
#
#   Q: "Can I get an extension on a report deadline?"
#                                                       MiniLM    bge-base
#   "Submission dates may be deferred on written        0.0000 !!  0.8818
#    application to the Director."       (the answer)
#   "The report deadline for the annual budget          0.1167     0.0004
#    review is 31 March."         (shares the words)
#
# MiniLM put the lexical decoy on top and scored the actual answer at
# zero - worse than the lexical reranker it was meant to replace, because
# it is confident about it. bge-reranker-base is ~2.5x slower per pair on
# CPU and reranking only ever sees RAG_RETRIEVAL_DEPTH candidates, so that
# cost is a few hundred milliseconds per question and it buys the entire
# reason this component exists.
DEFAULT_MODEL = "BAAI/bge-reranker-base"

# Scores are sigmoid outputs in (0, 1), forced rather than inherited (see
# the module docstring), so this floor is on the same scale as the Bedrock
# reranker's and means roughly the same thing.
#
# MEASURED, not inherited from a model card. Nine query/passage pairs
# through this class on bge-reranker-base:
#
#   clearly relevant (incl. paraphrase and a table row)   0.8166 - 0.9974
#   marginal (same topic, does not answer the question)   0.0001 - 0.0003
#   irrelevant                                            0.0000
#
# The distribution is bimodal to the point of being nearly binary, so any
# floor in the wide empty middle behaves identically. 0.30 sits there with
# room on both sides: far enough above the marginal band to exclude it
# outright, far enough below the relevant band that a genuinely responsive
# passage scored unusually low still reaches the answer.
#
# An earlier draft of this file declared 0.10 while defaulting to MiniLM,
# whose relevant passages scored 0.0000-0.1739 - so the floor would have
# admitted marginal passages and rejected real ones on the very model it
# was paired with. Re-measure this whenever the model changes; the two
# constants are one decision, not two.
DEFAULT_RELEVANCE_FLOOR = 0.30

# Pairs per forward pass. Smaller than the embedder's 32 because each item
# here is a query AND a passage rather than one text, and because a whole
# batch is at most RAG_RETRIEVAL_DEPTH candidates - there is no throughput
# to win at this size, only a memory spike to avoid.
DEFAULT_BATCH_SIZE = 16

# CPU unless told otherwise, for the reason the local embedder gives: with
# device unset, sentence-transformers takes CUDA whenever a GPU is visible,
# so a web worker quietly claims GPU memory from whatever else is using it.
# Note also that torch on CPU starts a thread per core regardless of the
# container's CPU limit; the local embedding provider's
# EMBEDDING_LOCAL_THREADS calls torch.set_num_threads, which is
# process-global and covers this model too when both are enabled.
DEFAULT_DEVICE = "cpu"

# The token budget for the query and the passage TOGETHER. Passed
# explicitly rather than left to the tokenizer: this model caps itself at
# 512 anyway, but bge-reranker-v2-m3 declares 8192, and attention is
# quadratic, so pointing RAG_RERANKER_LOCAL_MODEL at that model without
# this would turn a single long chunk into seconds of CPU per pair.
MAX_LENGTH = 512

# Roughly where that budget ends, at ~4 characters per token, less the room
# a query needs. Past it the tail of the passage is dropped silently.
MAX_PASSAGE_CHARS = 1_800


class LocalRerankUnavailable(RuntimeError):
    """sentence-transformers is missing, or the model will not load."""


def _sigmoid(value: float) -> float:
    """Logistic, written so it cannot overflow on the negative tail.

    math.exp(-value) raises OverflowError for a large negative logit, and
    large negative logits are the exact population this gets called on.
    """
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


class LocalCrossEncoderReranker:
    """Reorders retrieved candidates with a cross-encoder, in this process."""

    name = "local-cross-encoder"
    semantic = True

    default_relevance_floor = DEFAULT_RELEVANCE_FLOOR

    def __init__(self):
        self.model_id = (
            getattr(settings, "RAG_RERANKER_LOCAL_MODEL", "") or DEFAULT_MODEL
        )

        # The version IS the model id, as it is for the local embedding
        # provider. Two cross-encoders' scores are not comparable, so a
        # ranking recorded against a question is only interpretable next to
        # the model that produced it.
        self.version = self.model_id

        # predict() rejects a non-positive batch size with a ValueError, and
        # a mis-set environment variable should not be the thing that turns
        # reranking off across a whole deployment.
        self.batch_size = max(
            1,
            int(getattr(settings, "RAG_RERANKER_LOCAL_BATCH_SIZE", 0) or 0)
            or DEFAULT_BATCH_SIZE,
        )
        self.device = (
            getattr(settings, "RAG_RERANKER_LOCAL_DEVICE", "") or DEFAULT_DEVICE
        )

        # Same contract as the answerer's and the Bedrock reranker's: a
        # degraded path that needs no configuration of its own, so a failure
        # has somewhere to land.
        self._fallback = LexicalReranker()
        self._model = None
        self._lock = threading.Lock()

    # --- model -----------------------------------------------------------

    def _get_model(self):
        """The loaded cross-encoder, built once per process on first use.

        Double-checked under a lock because gunicorn's threads can reach the
        first rerank() at the same moment, and loading the weights twice
        concurrently is a memory spike big enough to get a container killed.
        """
        if self._model is not None:
            return self._model

        with self._lock:
            if self._model is not None:
                return self._model

            try:
                import torch
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise LocalRerankUnavailable(
                    "sentence-transformers is not installed. Install the CPU "
                    "build of torch FIRST - otherwise pip resolves the CUDA "
                    "wheel and pulls in several GB of nvidia packages this "
                    "reranker never uses:\n"
                    "  pip install -r backend/requirements-local.txt"
                ) from exc

            started = time.monotonic()

            try:
                model = load_cached_first(
                    lambda local_files_only: self._construct(
                        CrossEncoder,
                        torch.nn.Sigmoid(),
                        local_files_only=local_files_only,
                    ),
                    model_id=self.model_id,
                    refuse=(LocalRerankUnavailable,),
                )
            except LocalRerankUnavailable:
                raise
            except Exception as exc:
                # Nearly always a download that could not happen: no
                # network, no writable cache, or HF_HUB_OFFLINE set over an
                # empty one. Re-raised with the model id and device in the
                # message, because the underlying errors name a cache path
                # and leave the reader to guess which model wanted it.
                raise LocalRerankUnavailable(
                    f"Could not load {self.model_id} on {self.device}: {exc}"
                ) from exc

            logger.info(
                "rerank.model_loaded",
                extra={
                    "model": self.model_id,
                    "device": self.device,
                    "max_length": MAX_LENGTH,
                    "seconds": round(time.monotonic() - started, 2),
                },
            )

            self._model = model
            return self._model

    def _construct(self, cross_encoder, activation, *, local_files_only=False):
        """Build the model with its output activation forced to sigmoid.

        The keyword was renamed in sentence-transformers 4.0
        (`default_activation_function` becomes `activation_fn`), and
        requirements-local.txt still allows 3.x, where only the old name
        exists. Both are tried rather than either assumed, because the
        consequence of quietly losing this argument is not a crash: it is
        raw logits flowing into a threshold written for probabilities.
        """
        try:
            return cross_encoder(
                self.model_id,
                activation_fn=activation,
                max_length=MAX_LENGTH,
                device=self.device,
                local_files_only=local_files_only,
            )
        except TypeError:
            logger.info(
                "rerank.legacy_activation_keyword",
                extra={"model": self.model_id},
            )

        try:
            return cross_encoder(
                self.model_id,
                default_activation_function=activation,
                max_length=MAX_LENGTH,
                device=self.device,
                local_files_only=local_files_only,
            )
        except TypeError as exc:
            raise LocalRerankUnavailable(
                "This sentence-transformers accepts neither activation_fn "
                "nor default_activation_function, so the score range cannot "
                f"be pinned to [0, 1] for {self.model_id}: {exc}"
            ) from exc

    # --- reranking -------------------------------------------------------

    def rerank(self, query: str, candidates: list) -> list:
        if not candidates:
            return []

        if not (query or "").strip():
            # Nothing to score against. The lexical reranker already
            # handles this by returning everything at zero, and duplicating
            # that judgement here would be a second place to get it wrong.
            return self._fallback.rerank(query, candidates)

        try:
            return self._rerank(query, candidates)
        except Exception as exc:
            # Deliberately broad, for the reason the answerer's gateway
            # gives: a missing package, an unreachable hub, a corrupt cache
            # and an out-of-memory kill all mean the same thing to someone
            # waiting for search results, and none of them should be a 500
            # when a usable ordering is available for free.
            logger.warning(
                "rerank.fallback",
                extra={
                    "model": self.model_id,
                    "error": type(exc).__name__,
                    "candidates": len(candidates),
                },
            )
            degraded = self._fallback.rerank(query, candidates)
            for item in degraded:
                item.signals["reranker_degraded"] = (
                    f"{self.name} unavailable ({type(exc).__name__}); "
                    f"scored by {self._fallback.name} instead"
                )
            return degraded

    def _rerank(self, query: str, candidates: list) -> list:
        model = self._get_model()

        # Whitespace-only chunks never reach the model. It would score an
        # empty passage happily and the number would mean nothing; worse,
        # the pair list has to line up with the candidate list index for
        # index, so lifting them out here is also what keeps that mapping
        # honest.
        pairs = []
        pair_indexes = []
        truncated = set()

        for index, candidate in enumerate(candidates):
            text = (candidate.chunk.text or "").strip()
            if not text:
                continue
            if len(text) > MAX_PASSAGE_CHARS:
                truncated.add(index)
            pairs.append((query, text))
            pair_indexes.append(index)

        if truncated:
            logger.warning(
                "rerank.passage_truncated",
                extra={
                    "model": self.model_id,
                    "passages": len(truncated),
                    "limit_characters": MAX_PASSAGE_CHARS,
                    "detail": (
                        "the pair is truncated longest_first, so the query "
                        "survives and the tail of the passage is dropped"
                    ),
                },
            )

        by_index = {}
        repaired = False

        if pairs:
            # A list of 2-tuples, never a bare ("query", "passage"): given a
            # first element that is not itself a sequence of two, predict()
            # reads the whole argument as ONE pair and returns a scalar,
            # which then has no length to check and no order to preserve.
            raw = model.predict(
                pairs,
                batch_size=self.batch_size,
                # Defaults to None, which resolves to "on if logging is at
                # INFO or DEBUG" - that is, a tqdm bar sprayed through the
                # gunicorn logs on every single question asked.
                show_progress_bar=False,
                convert_to_numpy=True,
            )

            scores = raw.tolist() if hasattr(raw, "tolist") else list(raw)
            if not isinstance(scores, list) or len(scores) != len(pairs):
                raise LocalRerankUnavailable(
                    f"{self.model_id} returned {type(raw).__name__} for "
                    f"{len(pairs)} pairs, which is not one score per pair."
                )

            # numpy float32 is not a float as far as round() and the JSON
            # encoder behind the response are concerned.
            scores = [float(value) for value in scores]

            # The second half of the activation contract. _construct()
            # catches a keyword that has been RENAMED, because that raises
            # TypeError - but a version that accepted the argument and
            # ignored it would raise nothing at all, and the only visible
            # symptom would be a relevance floor that had quietly stopped
            # being one. Comparing two floats per candidate is cheap enough
            # that there is no reason to take the risk.
            if any(value < 0.0 or value > 1.0 for value in scores):
                repaired = True
                logger.warning(
                    "rerank.activation_repaired",
                    extra={
                        "model": self.model_id,
                        "detail": (
                            "scores arrived outside [0, 1] despite an "
                            "explicit sigmoid; applying it here instead"
                        ),
                    },
                )
                scores = [_sigmoid(value) for value in scores]

            # predict() sorts by length internally and un-sorts before
            # returning, so this is the input order.
            by_index = dict(zip(pair_indexes, scores, strict=True))

        scored = []
        for index, candidate in enumerate(candidates):
            # A candidate the model did not score is ranked last rather than
            # dropped. Dropping it would let an empty chunk silently delete
            # evidence, and the engine's relevance floor cannot apply a
            # floor to results it never saw - nor can the retrieval metrics,
            # which are computed over the full reranked ranking.
            score = by_index.get(index)

            signals = {
                "cross_encoder": self.model_id,
                "scored": score is not None,
                "retrieval_rank": index + 1,
            }
            if score is None:
                signals["reason"] = "passage has no text to score"
            if index in truncated:
                signals["truncated"] = (
                    f"passage exceeds the {MAX_LENGTH}-token pair budget; "
                    "scored on its opening only"
                )
            if repaired and score is not None:
                signals["activation_repaired"] = (
                    "model returned logits despite an explicit sigmoid; "
                    "converted to [0, 1] before ranking"
                )

            scored.append(
                RerankedChunk(
                    chunk=candidate.chunk,
                    score=round(score, 6) if score is not None else 0.0,
                    retrieval_score=candidate.score,
                    signals=signals,
                )
            )

        # Same deterministic tie-break as the lexical reranker, so a change
        # of reranker cannot change ordering for reasons unrelated to
        # relevance.
        scored.sort(key=lambda r: (-r.score, -r.retrieval_score, r.chunk.id))
        return scored

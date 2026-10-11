"""Cohere Rerank 3.5 on Bedrock, behind the same Reranker protocol.

Reranking is where precision is won or lost. Retrieval casts a wide net
cheaply; reranking decides which of that net's contents reach the context
window, and therefore what the answer is built from. The bundled
`LexicalReranker` scores term overlap - coverage, density, proximity - which
is a genuinely reasonable proxy and is completely blind to the thing that
matters most: whether the passage *answers* the question.

    Q: "Can I get an extension on a report deadline?"
    A passage reading "Submission dates may be deferred on written
    application to the Director" shares almost no terms with that question
    and answers it exactly. Lexical scoring ranks it near zero.

A cross-encoder reads the query and the passage together and scores that
relationship directly, which is why it is the single largest precision
improvement available to a RAG system that already has hybrid retrieval.

WHY THIS IS A RERANKER AND NOT A RETRIEVER. A cross-encoder must run the
model once per (query, passage) pair, so it cannot scan a corpus - scoring
twelve thousand documents per question is not a thing anyone can afford.
That is exactly why the two-stage shape exists: cheap retrieval narrows
tens of thousands of chunks to `RAG_RETRIEVAL_DEPTH` candidates, and the
expensive model only ever sees those.

SCORES ARE NOT COMPARABLE ACROSS RERANKERS, which is a trap worth naming.
`LexicalReranker` emits a weighted sum of three ratios; Cohere emits a
calibrated relevance probability. `RAG_RELEVANCE_FLOOR = 0.15` was chosen
against the first distribution and means something different against the
second. So this class declares its own `default_relevance_floor`, and the
engine prefers an explicitly configured floor, then the reranker's own,
then the module default - rather than silently applying one scoring
scheme's threshold to another's numbers.

FAILURE FALLS BACK RATHER THAN 500s, on the same principle as the answerer:
a reranking outage degrades result ordering, which is worth far less than
taking search down. The fallback is recorded in `name`, because a quietly
degraded ranking that does not say so is worse than a slow one.
"""

import logging
import os
import threading

from django.conf import settings

from .reranking import LexicalReranker, RerankedChunk

logger = logging.getLogger("adi.rerank")

MODEL_ID = "cohere.rerank-v3-5:0"

# Cohere Rerank 3.5 returns a calibrated relevance score in [0, 1]. A
# genuinely responsive passage typically lands well above 0.3 and an
# unrelated one well below 0.05, so this floor sits in the empty middle
# rather than cutting through either population. Re-measure it against the
# evaluation set before trusting it on a new corpus - it is a starting
# point, not a constant of nature.
DEFAULT_RELEVANCE_FLOOR = 0.10


class BedrockRerankUnavailable(RuntimeError):
    """boto3 is missing, or there is nothing to authenticate with."""


class BedrockReranker:
    """Reorders retrieved candidates with a cross-encoder."""

    name = "bedrock-cohere-rerank-3.5"
    # Scores read query and passage together (see context.build).
    semantic_scores = True
    semantic = True

    default_relevance_floor = DEFAULT_RELEVANCE_FLOOR

    def __init__(self):
        self.model_id = getattr(settings, "RAG_RERANKER_MODEL", "") or MODEL_ID
        self.version = self.model_id

        # Same contract as the answerer's: a degraded path that needs no
        # configuration of its own, so an outage has somewhere to land.
        self._fallback = LexicalReranker()
        self._client = None
        self._lock = threading.Lock()

    # --- client ----------------------------------------------------------

    def _region(self) -> str:
        region = (
            getattr(settings, "AWS_BEDROCK_REGION", "")
            or os.environ.get("AWS_REGION", "")
            or os.environ.get("AWS_DEFAULT_REGION", "")
        )
        if not region:
            raise BedrockRerankUnavailable(
                "Bedrock reranking needs a region. Set AWS_BEDROCK_REGION "
                "(or AWS_REGION) to a region where you have been granted "
                f"model access to {self.model_id}. Note that Amazon Rerank "
                "1.0 is not offered in us-east-1 while Cohere Rerank 3.5 is."
            )
        return region

    def _model_arn(self, region: str) -> str:
        """The ARN the Rerank API wants, built from the bare model id.

        Built here rather than demanded of whoever sets RAG_RERANKER_MODEL:
        the Rerank API takes a full ARN, every other Bedrock surface in
        this project takes a bare id, and making one setting mean two
        different things is how a deployment ends up with a 400 that says
        nothing useful. An ARN passed in explicitly is left alone.
        """
        if self.model_id.startswith("arn:"):
            return self.model_id
        return f"arn:aws:bedrock:{region}::foundation-model/{self.model_id}"

    def _get_client(self):
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
                raise BedrockRerankUnavailable(
                    "boto3 is not installed. "
                    "pip install -r backend/requirements-llm.txt"
                ) from exc

            config = Config(
                region_name=region,
                retries={
                    "max_attempts": int(
                        getattr(settings, "RAG_RERANKER_RETRIES", 0) or 3
                    ),
                    "mode": "adaptive",
                },
                read_timeout=int(getattr(settings, "RAG_RERANKER_TIMEOUT", 0) or 15),
                connect_timeout=5,
            )

            # NOT bedrock-runtime. Rerank lives on the agent runtime
            # endpoint, which is a different service entirely; calling it
            # on bedrock-runtime fails with an unknown-operation error that
            # reads like the model is missing.
            self._client = boto3.client("bedrock-agent-runtime", config=config)
            self._region_cached = region
            return self._client

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
            # gives: a throttle, an outage, a revoked credential and a
            # network blip all mean the same thing to someone waiting for
            # search results, and none should be a 500 when a usable
            # ordering is available for free.
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
        client = self._get_client()
        region = getattr(self, "_region_cached", "") or self._region()

        texts = [candidate.chunk.text for candidate in candidates]

        response = client.rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=[
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {
                        "type": "TEXT",
                        "textDocument": {"text": text},
                    },
                }
                for text in texts
            ],
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self._model_arn(region)},
                    # Every candidate, not a top-k. The engine's relevance
                    # floor is what decides which passages are good enough,
                    # and it cannot apply a floor to results it never saw.
                    # Truncating here would also corrupt the retrieval
                    # metrics, which are computed over the full reranked
                    # ranking.
                    "numberOfResults": len(texts),
                },
            },
        )

        by_index = {
            result["index"]: result["relevanceScore"]
            for result in response.get("results", [])
            if isinstance(result.get("index"), int)
        }

        if not by_index:
            raise BedrockRerankUnavailable(
                f"{self.model_id} returned no results for {len(texts)} candidates."
            )

        scored = []
        for index, candidate in enumerate(candidates):
            # A candidate the model did not score is ranked last rather
            # than dropped. Dropping it would let a paging boundary or a
            # partial response silently delete evidence, and an answer
            # built from silently-deleted evidence is the failure mode this
            # whole pipeline is arranged to prevent.
            score = by_index.get(index)
            scored.append(
                RerankedChunk(
                    chunk=candidate.chunk,
                    score=round(float(score), 6) if score is not None else 0.0,
                    retrieval_score=candidate.score,
                    signals={
                        "cross_encoder": self.model_id,
                        "scored": score is not None,
                        "retrieval_rank": index + 1,
                    },
                )
            )

        # Same deterministic tie-break as the lexical reranker, so a change
        # of reranker cannot change ordering for reasons unrelated to
        # relevance.
        scored.sort(key=lambda r: (-r.score, -r.retrieval_score, r.chunk.id))
        return scored

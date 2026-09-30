FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime packages only. The image used to install build-essential and
# libpq-dev as well - roughly 200 MB of compiler toolchain and headers that
# nothing uses at runtime (every Python dependency ships a prebuilt wheel,
# and psycopg[binary] bundles its own libpq), and a compiler sitting in a
# production image is a gift to anyone who gets a shell in it.
#
#   postgresql-client  pg_dump / pg_restore for manage.py backup and restore
#   poppler-utils      PDF page rendering for OCR
#   tesseract-ocr      OCR
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client \
        poppler-utils \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# An unprivileged user for everything the container runs. Created before
# the dependency layers so the uid is stable across rebuilds; the writable
# paths are created and handed to it at the end of this file.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app

COPY backend/requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

# Optional ML layer for running a trained classifier in-process. Off by
# default so a deployment that does not classify carries none of the
# weight: scikit-learn and its dependencies add ~200 MB to the image.
#
#   docker compose build --build-arg INSTALL_ML=true
#   INSTALL_ML=true docker compose up --build
ARG INSTALL_ML=false

COPY backend/requirements-ml.txt /app/requirements-ml.txt

RUN if [ "$INSTALL_ML" = "true" ]; then \
        pip install --no-cache-dir -r /app/requirements-ml.txt; \
    else \
        echo "Skipping ML dependencies (INSTALL_ML=$INSTALL_ML)"; \
    fi

# Optional LLM layer: the Anthropic SDK and boto3. Needed by all three of
# the Bedrock-backed components - semantic embeddings, cross-encoder
# reranking and generative answering - which share one set of AWS
# credentials. Off by default for the same reason as the ML layer: a
# deployment running the bundled baselines should not carry the weight.
#
#   docker compose build --build-arg INSTALL_LLM=true
#   INSTALL_LLM=true docker compose up --build
#
# This installs the CLIENTS, not the credentials. Without AWS credentials
# and per-region model access, the providers stay unconfigured and the
# platform runs its baselines - which is the intended failure mode.
ARG INSTALL_LLM=false

COPY backend/requirements-llm.txt /app/requirements-llm.txt

RUN if [ "$INSTALL_LLM" = "true" ]; then \
        pip install --no-cache-dir -r /app/requirements-llm.txt; \
    else \
        echo "Skipping LLM dependencies (INSTALL_LLM=$INSTALL_LLM)"; \
    fi

# Optional local-model layer: torch plus sentence-transformers, for the
# embedding provider and cross-encoder reranker that run in-process with no
# API key and no per-call cost. The heaviest of the three optional layers
# by a wide margin - CPU-only torch is several hundred megabytes - so it is
# off by default and worth building only for the services that embed.
#
#   docker compose build --build-arg INSTALL_LOCAL=true
#   INSTALL_LOCAL=true docker compose up --build
#
# Model WEIGHTS are not baked in; they download on first use into HF_HOME,
# which is set below to a path that can be mounted as a volume so the
# download survives a container rebuild.
ARG INSTALL_LOCAL=false

COPY backend/requirements-local.txt /app/requirements-local.txt

RUN if [ "$INSTALL_LOCAL" = "true" ]; then \
        pip install --no-cache-dir -r /app/requirements-local.txt; \
    else \
        echo "Skipping local model dependencies (INSTALL_LOCAL=$INSTALL_LOCAL)"; \
    fi

# Where sentence-transformers caches downloaded weights. Named explicitly
# so it can be a mounted volume: without one, every rebuilt container
# re-downloads the same model, and a worker that re-downloads 130 MB on
# each deploy is a worker that looks broken on a slow connection.
ENV HF_HOME=/app/.cache/huggingface

COPY backend /app/backend

# The evaluation corpus and its governed benchmark template. Small, text
# only, and part of the artifact on purpose: a regression gate that cannot
# find its own dataset inside the image it is gating is a gate that skips.
COPY evaluation /app/evaluation

# The only paths the application writes to, owned by the unprivileged user.
# The code itself stays root-owned and read-only to the process: a
# compromised worker cannot rewrite the application it is running.
#
# A named volume mounted on one of these starts as a copy of the directory,
# ownership included, so a fresh volume just works. A volume created by an
# older, root-running image keeps root ownership - see
# docs/deployment/production.md for the one-time chown.
RUN mkdir -p /app/backend/media /app/backend/staticfiles /app/.cache/huggingface /backups \
    && chown -R app:app /app/backend/media /app/backend/staticfiles /app/.cache /backups

USER app

WORKDIR /app/backend

EXPOSE 8000

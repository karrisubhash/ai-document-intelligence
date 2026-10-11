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

# Every install below runs against backend/constraints.txt: the exact versions
# the tested image contained. Without it a rebuild installed whatever was
# newest that day, so staging and production could run code nobody tested.
COPY backend/constraints.txt /app/constraints.txt
COPY backend/requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r requirements.txt -c /app/constraints.txt

# Optional ML layer for running a trained classifier in-process. Off by
# default so a deployment that does not classify carries none of the
# weight: scikit-learn and its dependencies add ~200 MB to the image.
#
#   docker compose build --build-arg INSTALL_ML=true
#   INSTALL_ML=true docker compose up --build
ARG INSTALL_ML=false

COPY backend/requirements-ml.txt /app/requirements-ml.txt

RUN if [ "$INSTALL_ML" = "true" ]; then \
        pip install --no-cache-dir -r /app/requirements-ml.txt -c /app/constraints.txt; \
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
        pip install --no-cache-dir -r /app/requirements-llm.txt -c /app/constraints.txt; \
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
        pip install --no-cache-dir -r /app/requirements-local.txt -c /app/constraints.txt; \
    else \
        echo "Skipping local model dependencies (INSTALL_LOCAL=$INSTALL_LOCAL)"; \
    fi

# Optional S3 layer: django-storages, for AWS_STORAGE_BUCKET_NAME (S3 or any
# S3-compatible store such as MinIO). Settings select the S3 backend whenever
# a bucket is configured, and no image used to install it - so turning S3 on
# failed at the first file operation.
#
#   INSTALL_S3=true docker compose -f docker-compose.yml -f docker-compose.prod.yml build
ARG INSTALL_S3=false

COPY backend/requirements-s3.txt /app/requirements-s3.txt

RUN if [ "$INSTALL_S3" = "true" ]; then \
        pip install --no-cache-dir -r /app/requirements-s3.txt -c /app/constraints.txt; \
    else \
        echo "Skipping S3 dependencies (INSTALL_S3=$INSTALL_S3)"; \
    fi

# Distributed tracing (spec 20), optional: OpenTelemetry SDK, the OTLP
# exporter and Django/Celery/psycopg instrumentation. Inert until
# OTEL_EXPORTER_OTLP_ENDPOINT is set; see documents/observability/tracing.py.
#
#   INSTALL_OTEL=true docker compose -f docker-compose.yml -f docker-compose.prod.yml build
ARG INSTALL_OTEL=false

COPY backend/requirements-otel.txt /app/requirements-otel.txt

RUN if [ "$INSTALL_OTEL" = "true" ]; then         pip install --no-cache-dir -r /app/requirements-otel.txt -c /app/constraints.txt;     else         echo "Skipping tracing dependencies (INSTALL_OTEL=$INSTALL_OTEL)";     fi

# The base image's pip (25.0.1) carries published advisories, and a scanner
# reads the final filesystem. Upgraded after the dependency layers so the
# pinned installs above stay cached; nothing at runtime runs pip.
RUN pip install --no-cache-dir "pip==26.2.1"

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

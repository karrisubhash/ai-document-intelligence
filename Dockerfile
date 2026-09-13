FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        postgresql-client \
        poppler-utils \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

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

COPY backend /app/backend

# The evaluation corpus and its governed benchmark template. Small, text
# only, and part of the artifact on purpose: a regression gate that cannot
# find its own dataset inside the image it is gating is a gate that skips.
COPY evaluation /app/evaluation

WORKDIR /app/backend

EXPOSE 8000

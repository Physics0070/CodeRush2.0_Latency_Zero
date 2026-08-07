# syntax=docker/dockerfile:1

# ---------- stage 1: build the React frontend ----------
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# ---------- stage 2: backend + built frontend, one origin ----------
FROM python:3.11-slim AS runtime

# libmagic1 backs python-magic (magic-byte upload validation, not content_type).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libmagic1 curl \
 && rm -rf /var/lib/apt/lists/*

# HF Spaces runs as a non-root user; matching that locally avoids permission
# surprises at deploy time.
RUN useradd -m -u 1000 app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/home/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/home/app/.cache/sentence-transformers

WORKDIR /app

# Semantic metrics (MIG, duplicate detection) are opt-in. torch is ~1GB and is
# served from download.pytorch.org, which refuses or stalls connections on some
# networks and CI builders - a deploy should not fail for a reason unrelated to
# this app. Without it the metrics report `embeddings_available: false` and omit
# those two numbers; the chat, council, replay and everything else are
# unaffected.
#   docker build --build-arg WITH_EMBEDDINGS=1 -t aco .
ARG WITH_EMBEDDINGS=0

COPY requirements.txt requirements-embeddings.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN if [ "$WITH_EMBEDDINGS" = "1" ]; then \
      pip install --no-cache-dir --retries 8 --timeout 60 \
        -r requirements-embeddings.txt \
        --extra-index-url https://download.pytorch.org/whl/cpu ; \
    fi

COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app --from=frontend /build/dist ./frontend/dist

USER app

# Bake the embedding model in so the first metrics request is not a 90-second
# download on a cold Space. Skipped when embeddings were not installed.
ARG WITH_EMBEDDINGS
RUN if [ "$WITH_EMBEDDINGS" = "1" ]; then \
      python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')" ; \
    fi

# HF Spaces hardcodes 7860.
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:7860/health || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7860"]

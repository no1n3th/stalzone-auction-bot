# syntax=docker/dockerfile:1
# ---------- Stage 1: build frontend SPA ----------
FROM node:20-alpine AS frontend

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite.config.ts emits to ../web/static/spa -> /build/web/static/spa
RUN npm run build

# ---------- Stage 2: python runtime ----------
FROM python:3.12-slim AS runtime

ENV MALLOC_ARENA_MAX=2 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Backend source
COPY config ./config
COPY database ./database
COPY migrations ./migrations
COPY models ./models
COPY scanner ./scanner
COPY analytics ./analytics
COPY api ./api
COPY deals ./deals
COPY notify ./notify
COPY metrics ./metrics
COPY utils ./utils
COPY web ./web
COPY scripts ./scripts
COPY alembic.ini main.py ./

# Prebuilt SPA from stage 1 (overrides the copy above's web/static if any)
COPY --from=frontend /build/web/static/spa ./web/static/spa

RUN mkdir -p /app/data /app/logs && chown -R app:app /app

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/live').status==200 else 1)"

CMD ["python", "main.py"]

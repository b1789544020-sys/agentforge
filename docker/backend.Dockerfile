# ── AgentForge backend image ─────────────────────────────────
# Build context: ./backend
# Built by docker compose; migrations run in the separate migrate service.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENTFORGE_SANDBOX_MODE=docker

WORKDIR /app

# Install OS deps that the FastAPI / SQLAlchemy stack commonly needs
# (curl for healthchecks, build-essential for any wheel that fails to
# ship a manylinux build).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl build-essential \
 && rm -rf /var/lib/apt/lists/*

# Layer 1: copy + install requirements first to maximize docker cache.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Layer 2: copy the rest of the backend source.
RUN groupadd --system agentforge && useradd --system --gid agentforge agentforge
COPY --chown=agentforge:agentforge . /app/backend
RUN mkdir -p /app/data /app/backend/storage && chown -R agentforge:agentforge /app/data /app/backend/storage

# `app.main:app` expects the working directory to be /app, where
# `app/` is a sibling.  Add both /app and /app/backend to PYTHONPATH.
ENV PYTHONPATH="/app:/app/backend"

EXPOSE 8000

USER agentforge
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────────
# Web RAG Application – Docker image
# ─────────────────────────────────────────────────────────────────────
# Build:
#   docker build -t web-rag-app .
#
# Run (pass secrets via --env-file or -e):
#   docker run --rm -p 5000:5000 --env-file .env web-rag-app
#
# The container:
#   • exposes port 5000
#   • runs Flask via `python app.py`
#   • starts with an empty SQLite DB on every launch (the DB file lives
#     inside the container, not in a mounted volume, which satisfies the
#     "clean state on every new launch" requirement)
# ─────────────────────────────────────────────────────────────────────

FROM python:3.11-slim AS base

# ── Python environment ───────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ── Python dependencies (cached layer) ───────────────────────────────
# Copy only the requirements file first so Docker can reuse this layer
# across rebuilds when application code changes but deps do not.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ─────────────────────────────────────────────────
COPY . .

# ── Non-root runtime user ────────────────────────────────────────────
# Security best practice: do not run the Flask process as root.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

# ── Networking & entrypoint ──────────────────────────────────────────
EXPOSE 5000

# These two defaults make the app reachable from outside the container
# regardless of what the host's .env file specifies.
ENV FLASK_HOST=0.0.0.0 \
    FLASK_PORT=5000

CMD ["python", "app.py"]

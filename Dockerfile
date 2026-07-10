FROM python:3.12.7-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Run as a non-root user; /app/data holds the SQLite DB (bind-mounted at runtime).
RUN useradd -m appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# Liveness probe hits the unauthenticated /health route (webhook mode). Uses the
# stdlib so the slim image needs no extra packages. Port matches PORT=8080.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status==200 else 1)"

# Entry point. RUN_MODE (webhook in prod) is pinned in docker-compose.yml.
CMD ["python", "-m", "app.main"]

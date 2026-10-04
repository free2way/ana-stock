FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt requirements-lock.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements-lock.txt

COPY app ./app
COPY scripts ./scripts
COPY README.md .

# Run as an unprivileged user; bind mounts for storage/data must stay writable
# by uid 10001 on Linux hosts (Docker Desktop file sharing maps access).
RUN mkdir -p /app/storage /app/data/raw /app/data/normalized /app/data/qlib /app/data/artifacts && \
    useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "python scripts/init_db.py && uvicorn app.api.main:app --host 0.0.0.0 --port 8000"]

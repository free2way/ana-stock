FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY README.md .

RUN mkdir -p /app/storage /app/data/raw /app/data/normalized /app/data/qlib /app/data/artifacts

EXPOSE 8000

CMD ["sh", "-c", "python scripts/init_db.py && uvicorn app.api.main:app --host 0.0.0.0 --port 8000"]

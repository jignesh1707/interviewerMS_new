FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=8080 \
    STORAGE_DIR=/data \
    DATABASE_PATH=/data/interviews.db

WORKDIR /app

COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY app ./app
COPY models.yaml plans.yaml ./

# Run as an unprivileged user; /data is the only writable location.
RUN useradd --system --uid 10001 --no-create-home appuser \
    && mkdir /data && chown appuser /data
USER appuser
VOLUME ["/data"]
EXPOSE 8080

# ENVIRONMENT=production makes the app refuse to start without strong API_KEYS and explicit CORS_ORIGINS.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ.get('PORT','8080'),timeout=4)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\""]

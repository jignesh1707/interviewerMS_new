FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=8080 \
    STORAGE_DIR=/data \
    DATABASE_PATH=/data/interviews.db \
    MODELS_DIR=/opt/models \
    HOME=/tmp \
    HF_HOME=/tmp/hf \
    HF_HUB_OFFLINE=1

WORKDIR /app

# libgomp1 is needed by onnxruntime (Piper) on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock.txt requirements-voice.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt -r requirements-voice.txt

# Bake the speech models into the image so machines start without internet access and without
# downloading on the first interview. HF_HUB_OFFLINE is switched off for this one step only.
ARG WHISPER_REPO=Systran/faster-whisper-base
ARG PIPER_VOICE=en_US-lessac-medium
RUN HF_HUB_OFFLINE=0 python -c "\
import os; \
from huggingface_hub import snapshot_download, hf_hub_download; \
root = os.environ['MODELS_DIR']; \
snapshot_download('${WHISPER_REPO}', local_dir=root + '/whisper'); \
voice = '${PIPER_VOICE}'; \
lang, rest = voice.split('-', 1); \
name, quality = rest.rsplit('-', 1); \
base = 'en/' + lang + '/' + name + '/' + quality + '/' + voice; \
[hf_hub_download('rhasspy/piper-voices', base + ext, local_dir=root + '/piper') for ext in ('.onnx', '.onnx.json')]" \
    && find /opt/models/piper -mindepth 2 -name '*.onnx*' -exec mv -t /opt/models/piper {} + \
    && rm -rf /opt/models/piper/en /opt/models/whisper/.cache /opt/models/piper/.cache

ENV WHISPER_MODEL=/opt/models/whisper \
    PIPER_MODEL_PATH=/opt/models/piper/${PIPER_VOICE}.onnx

COPY app ./app
COPY models.yaml plans.yaml ./

# Run as an unprivileged user; /data is the only writable location (plus /tmp).
RUN useradd --system --uid 10001 --no-create-home appuser \
    && mkdir /data && chown appuser /data
USER appuser
VOLUME ["/data"]
EXPOSE 8080

# ENVIRONMENT=production makes the app refuse to start without strong API_KEYS and explicit CORS_ORIGINS.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ.get('PORT','8080'),timeout=4)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\""]

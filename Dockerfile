FROM python:3.12-slim

# ffmpeg converts/splits audio and lets yt-dlp extract audio from video links.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Set to "true" (Render: Docker build arg / local: --build-arg) to bundle local Whisper.
ARG LOCAL_WHISPER=false
COPY requirements.txt requirements-local-whisper.txt ./
RUN if [ "$LOCAL_WHISPER" = "true" ]; then \
      pip install --no-cache-dir -r requirements-local-whisper.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY backend ./backend
COPY frontend ./frontend

ENV PYTHONUNBUFFERED=1
# Render provides $PORT. One worker: jobs are kept in memory.
CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]

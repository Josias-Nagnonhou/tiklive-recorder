FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data \
    MALLOC_ARENA_MAX=2 \
    PYTHONUNBUFFERED=1
# yt-dlp est mis à jour à chaque démarrage : TikTok change souvent, ça évite les pannes
CMD pip install -q -U yt-dlp; uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}

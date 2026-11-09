# Dahot Tool download engine: FastAPI/yt-dlp app + bgutil PO-token
# provider sidecar (bgutil-ytdlp-pot-provider 2.0.2, server and the
# pip-installed yt-dlp plugin kept in lockstep — see requirements.txt).
# The provider sources live in pot/ and are compiled at BUILD time; at
# runtime start.sh launches it on 127.0.0.1:4416 next to the engine.
FROM python:3.12-slim

# ffmpeg for the engine; Node.js >= 22 for the provider (upstream
# requires it, and bookworm's own nodejs is too old, so use the
# NodeSource 22.x line).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (includes the bgutil yt-dlp plugin, pinned 2.0.2).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Provider deps first (layer caching), then its sources, then compile
# to pot/build/ at build time so container start stays instant and the
# 256 MB free-tier RAM never pays for a TypeScript build at runtime.
COPY pot/package.json pot/package-lock.json ./pot/
RUN cd pot && npm ci --no-audit --no-fund
COPY pot/src ./pot/src
COPY pot/types ./pot/types
COPY pot/tsconfig.json ./pot/tsconfig.json
RUN cd pot && npx tsc

COPY app.py .
COPY start.sh .

ENV PORT=7860
EXPOSE 7860
CMD ["sh", "start.sh"]

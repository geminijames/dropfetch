# Use the official BgUtils PO-token provider image as the runtime base.
# It already contains Node.js and the provider server. DropFetch adds Python,
# yt-dlp, FastAPI and FFmpeg in the same Render service.
FROM brainicism/bgutil-ytdlp-pot-provider:2.0.0

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-pip ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/dropfetch

COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV POT_PROVIDER_HOST=127.0.0.1
ENV POT_PROVIDER_PORT=4416

EXPOSE 10000

# The BgUtils base image has its own Node ENTRYPOINT. Clear it so this
# combined image can start both the local PO-token provider and DropFetch.
ENTRYPOINT []

# Start the local PO-token provider first, then the public DropFetch API.
CMD ["sh", "-c", "node /app/build/main.js --host 127.0.0.1 --port 4416 >/tmp/bgutil-provider.log 2>&1 & exec python3 -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]

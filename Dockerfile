FROM brainicism/bgutil-ytdlp-pot-provider:2.0.0

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-pip \
       ffmpeg \
       ca-certificates \
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

# IMPORTANT:
# BgUtils has its own Node ENTRYPOINT.
# We must remove it so our combined container can start correctly.
ENTRYPOINT []

# Start BgUtils PO-token provider first,
# then start the DropFetch FastAPI server.
CMD ["sh", "-c", "node /app/build/main.js --host 127.0.0.1 --port 4416 >/tmp/bgutil-provider.log 2>&1 & exec python3 -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]

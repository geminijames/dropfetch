import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import HttpUrl, BaseModel

APP_DIR = Path(__file__).resolve().parent
MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "7200"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "1024"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "3"))

app = FastAPI(title="DropFetch", version="3.0.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
last_requests = {}

class URLRequest(BaseModel):
    url: HttpUrl

def validate_public_url(url: str):
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise HTTPException(400, "Enter a valid http/https URL.")

    host = p.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise HTTPException(400, "Local/private URLs are not allowed.")

    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise HTTPException(400, "Private or local network URLs are not allowed.")
    except socket.gaierror:
        raise HTTPException(400, "The URL host could not be resolved.")

def rate_limit(request: Request):
    client = request.client.host if request.client else "unknown"
    now = time.time()
    if now - last_requests.get(client, 0) < RATE_LIMIT_SECONDS:
        raise HTTPException(429, "Please wait a moment before trying again.")
    last_requests[client] = now

def safe_name(value: str, fallback="download"):
    value = re.sub(r'[\\/:*?"<>|]+', "_", value or fallback)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:150] or fallback

def base_ydl():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 8,
    }

async def run_ydl(url, opts):
    def worker():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=opts.get("skip_download") is not True)
    return await asyncio.to_thread(worker)

@app.get("/")
async def index():
    return FileResponse(APP_DIR / "static" / "index.html")

@app.get("/api/health")
async def health():
    return {"ok": True, "service": "DropFetch"}

@app.post("/api/info")
async def info(payload: URLRequest, request: Request):
    rate_limit(request)
    url = str(payload.url)
    validate_public_url(url)

    opts = base_ydl()
    opts["skip_download"] = True

    try:
        data = await run_ydl(url, opts)
        duration = data.get("duration")
        if duration and duration > MAX_DURATION_SECONDS:
            raise HTTPException(400, "This media is longer than the allowed limit.")

        return {
            "title": data.get("title") or "Untitled",
            "thumbnail": data.get("thumbnail"),
            "duration": duration,
            "uploader": data.get("uploader") or data.get("channel"),
            "site": data.get("extractor_key") or data.get("extractor"),
            "webpage_url": data.get("webpage_url") or url,
            "kind": data.get("_type") or "video",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not inspect this link: {str(e)[:500]}")

@app.get("/api/download")
async def download(request: Request, url: str, media: str = "video", quality: str = "best"):
    rate_limit(request)
    validate_public_url(url)

    if media not in {"video", "audio", "image", "file"}:
        raise HTTPException(400, "Unsupported download type.")
    if quality not in {"best", "1080", "720", "480", "360"}:
        quality = "best"

    temp_dir = Path(tempfile.mkdtemp(prefix="dropfetch_"))

    try:
        async with semaphore:
            if media == "file":
                return await direct_file_download(url, temp_dir)

            if media == "image":
                return await image_download(url, temp_dir)

            output = str(temp_dir / "%(title).150s.%(ext)s")
            opts = base_ydl()
            opts.update({
                "outtmpl": output,
                "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
                "restrictfilenames": False,
                "overwrites": True,
            })

            if media == "audio":
                opts.update({
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                })
                expected_ext = ".mp3"
                mime = "audio/mpeg"
            else:
                if quality == "best":
                    fmt = "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best"
                else:
                    fmt = f"bestvideo[height<={quality}][ext=mp4]+bestaudio/best[height<={quality}][ext=mp4]/best[height<={quality}]"
                opts.update({
                    "format": fmt,
                    "merge_output_format": "mp4",
                })
                expected_ext = ".mp4"
                mime = "video/mp4"

            info = await run_ydl(url, opts)
            candidates = [p for p in temp_dir.iterdir() if p.is_file()]
            if not candidates:
                raise RuntimeError("No output file was produced.")

            candidates.sort(key=lambda p: (p.stat().st_size, p.stat().st_mtime), reverse=True)
            result = candidates[0]
            filename = safe_name(info.get("title", "video")) + expected_ext

            return FileResponse(
                result,
                media_type=mime,
                filename=filename,
                background=None,
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Download failed: {str(e)[:700]}")

async def image_download(url: str, temp_dir: Path):
    # For social pages, use the extractor's best thumbnail/image URL.
    opts = base_ydl()
    opts["skip_download"] = True
    info = await run_ydl(url, opts)
    image_url = info.get("thumbnail")

    if not image_url:
        # Try direct image URL as a fallback.
        image_url = url

    target = temp_dir / "image"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        async with client.stream("GET", image_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if not ctype.startswith("image/"):
                raise RuntimeError("The page did not expose a downloadable image.")
            suffix = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                "image/gif": ".gif", "image/avif": ".avif"
            }.get(ctype.split(";")[0].lower(), ".jpg")
            target = target.with_suffix(suffix)
            total = 0
            with target.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 128):
                    total += len(chunk)
                    if total > MAX_FILE_SIZE_MB * 1024 * 1024:
                        raise RuntimeError("Image exceeds the file-size limit.")
                    f.write(chunk)

    name = safe_name(info.get("title", "image")) + target.suffix
    return FileResponse(target, media_type=ctype, filename=name)

async def direct_file_download(url: str, temp_dir: Path):
    target = temp_dir / "file"
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "application/octet-stream")
            disposition = r.headers.get("content-disposition", "")
            filename = None
            m = re.search(r'filename="?([^";]+)"?', disposition, flags=re.I)
            if m:
                filename = safe_name(m.group(1))

            suffix = Path(urlparse(str(r.url)).path).suffix[:10]
            if not filename:
                filename = "download" + suffix

            total = 0
            with target.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 128):
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError("File exceeds the server's size limit.")
                    f.write(chunk)

    return FileResponse(target, media_type=ctype, filename=filename)

@app.on_event("startup")
async def startup():
    # Nothing persistent is intentionally stored.
    pass

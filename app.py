import asyncio
import ipaddress
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import HttpUrl, BaseModel

APP_DIR = Path(__file__).resolve().parent

MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "7200"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "1024"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "3"))

app = FastAPI(title="DropFetch", version="4.1.0")

POT_PROVIDER_HOST = os.getenv("POT_PROVIDER_HOST", "127.0.0.1")
POT_PROVIDER_PORT = int(os.getenv("POT_PROVIDER_PORT", "4416"))
POT_PROVIDER_URL = f"http://{POT_PROVIDER_HOST}:{POT_PROVIDER_PORT}"
pot_provider_process = None
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
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise HTTPException(400, "Private or local network URLs are not allowed.")
    except socket.gaierror:
        raise HTTPException(400, "The URL host could not be resolved.")


def rate_limit(request: Request, bucket: str = "default"):
    client = request.client.host if request.client else "unknown"
    key = (client, bucket)
    now = time.time()
    if now - last_requests.get(key, 0) < RATE_LIMIT_SECONDS:
        raise HTTPException(429, "Please wait a moment before trying again.")
    last_requests[key] = now


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
        "extractor_args": {
            "youtubepot-bgutilhttp": {"base_url": POT_PROVIDER_URL},
        },
    }


async def run_ydl(url, opts):
    def worker():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(
                url, download=opts.get("skip_download") is not True
            )

    return await asyncio.to_thread(worker)


@app.get("/")
async def index():
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/favicon.ico")
async def favicon():
    # A tiny SVG favicon avoids the old 404 in the Render logs.
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
    <defs><linearGradient id="g" x1="0" x2="1"><stop stop-color="#8b5cf6"/><stop offset="1" stop-color="#2ce0aa"/></linearGradient></defs>
    <rect width="64" height="64" rx="16" fill="#080b14"/>
    <path d="M18 18h19l9 9v19H18z" fill="url(#g)"/>
    <path d="M37 18v11h9M24 39h16M24 32h8" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>"""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "DropFetch", "version": "4.1.0", "youtube_pot_provider": True}


@app.post("/api/info")
async def info(payload: URLRequest, request: Request):
    rate_limit(request, "info")
    url = str(payload.url)
    validate_public_url(url)

    opts = base_ydl()
    opts["skip_download"] = True

    try:
        data = await run_ydl(url, opts)
        duration = data.get("duration")
        if duration and duration > MAX_DURATION_SECONDS:
            raise HTTPException(400, "This media is longer than the allowed limit.")

        thumbnail = data.get("thumbnail")
        proxy = None
        if thumbnail:
            proxy = "/api/thumbnail?src=" + quote(thumbnail, safe="")

        return {
            "title": data.get("title") or "Untitled",
            "thumbnail": thumbnail,
            "thumbnail_proxy": proxy,
            "duration": duration,
            "uploader": data.get("uploader") or data.get("channel"),
            "site": data.get("extractor_key") or data.get("extractor"),
            "webpage_url": data.get("webpage_url") or url,
            "kind": data.get("_type") or "video",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            400, f"Could not inspect this link: {str(e)[:500]}"
        )


@app.get("/api/thumbnail")
async def thumbnail(request: Request, src: str):
    # Thumbnail loads happen immediately after /api/info, so keep a separate
    # bucket instead of blocking the preview because the user just inspected.
    rate_limit(request, "thumbnail")
    validate_public_url(src)

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/153.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=20
        ) as client:
            r = await client.get(src, headers=headers)
            r.raise_for_status()

        ctype = r.headers.get("content-type", "").split(";")[0].lower()
        if not ctype.startswith("image/"):
            raise HTTPException(400, "Preview source did not return an image.")

        allowed = {
            "image/jpeg", "image/png", "image/webp",
            "image/gif", "image/avif"
        }
        if ctype not in allowed:
            raise HTTPException(400, "Unsupported preview image type.")

        return Response(
            content=r.content,
            media_type=ctype,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "Preview image is unavailable.")


@app.get("/api/download")
async def download(
    request: Request,
    url: str,
    media: str = "video",
    quality: str = "best",
    name: str = "",
):
    """Start a real browser download.

    Important: this endpoint intentionally returns StreamingResponse instead of
    FileResponse after all processing is finished. That lets Chrome receive the
    Content-Disposition headers immediately and create a native download item
    while the server prepares/streams the file.
    """
    rate_limit(request, "download")
    validate_public_url(url)

    if media not in {"video", "audio", "image", "file"}:
        raise HTTPException(400, "Unsupported download type.")
    if quality not in {"best", "1080", "720", "480", "360"}:
        quality = "best"

    requested_name = safe_name(name) if name.strip() else ""
    requested_stem = Path(requested_name).stem if requested_name else ""

    temp_dir = Path(tempfile.mkdtemp(prefix="dropfetch_"))

    async def stream_file(path: Path, filename: str, media_type: str):
        # The endpoint has already sent its HTTP headers before this generator
        # does any expensive work. Chrome therefore shows the download in its
        # normal Downloads UI instead of waiting for the final file.
        ready = asyncio.Event()
        state = {"path": None, "error": None}

        async def prepare():
            try:
                async with semaphore:
                    if media == "file":
                        await stream_direct_source(url, temp_dir, state)
                    elif media == "image":
                        await stream_image_source(url, temp_dir, state)
                    else:
                        output = str(temp_dir / "%(title).150s.%(ext)s")
                        opts = base_ydl()
                        opts.update(
                            {
                                "outtmpl": output,
                                "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
                                "restrictfilenames": False,
                                "overwrites": True,
                                # Write the target progressively instead of a
                                # .part file so the browser can receive bytes
                                # while yt-dlp is still downloading.
                                "nopart": True,
                            }
                        )

                        if media == "audio":
                            opts.update(
                                {
                                    "format": "bestaudio/best",
                                    "postprocessors": [
                                        {
                                            "key": "FFmpegExtractAudio",
                                            "preferredcodec": "mp3",
                                            "preferredquality": "192",
                                        }
                                    ],
                                }
                            )
                            expected_ext = ".mp3"
                        else:
                            if quality == "best":
                                fmt = "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best"
                            else:
                                fmt = (
                                    f"bestvideo[height<={quality}][ext=mp4]+bestaudio/"
                                    f"best[height<={quality}][ext=mp4]/best[height<={quality}]"
                                )
                            opts.update({"format": fmt, "merge_output_format": "mp4"})
                            expected_ext = ".mp4"

                        info = await run_ydl(url, opts)
                        candidates = [p for p in temp_dir.iterdir() if p.is_file()]
                        if not candidates:
                            raise RuntimeError("No output file was produced.")
                        candidates.sort(
                            key=lambda p: (p.stat().st_size, p.stat().st_mtime),
                            reverse=True,
                        )
                        result = candidates[0]
                        state["path"] = result
                        state["filename"] = safe_name(info.get("title", "video")) + expected_ext
            except Exception as exc:
                state["error"] = str(exc)[:700]
            finally:
                ready.set()

        task = asyncio.create_task(prepare())
        try:
            # Give the response a chance to send headers immediately. For
            # native browser downloads, the Content-Disposition header is the
            # important part; the first body bytes can arrive later.
            await asyncio.sleep(0)
            await ready.wait()
            if state["error"]:
                raise RuntimeError(state["error"])

            final_path = state.get("path") or path
            if not final_path or not final_path.exists():
                raise RuntimeError("No output file was produced.")

            with final_path.open("rb") as f:
                while True:
                    chunk = f.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            if not task.done():
                task.cancel()
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    # For native browser downloads, use a stable placeholder filename in the
    # response. yt-dlp will replace it internally with the actual title; the
    # fallback is still a valid downloaded filename.
    if media == "audio":
        fallback_name, mime = (requested_stem + ".mp3" if requested_stem else "dropfetch_audio.mp3"), "audio/mpeg"
    elif media == "image":
        fallback_name, mime = (requested_stem + ".jpg" if requested_stem else "dropfetch_image.jpg"), "image/jpeg"
    elif media == "file":
        fallback_name, mime = (requested_name if requested_name else "dropfetch_file"), "application/octet-stream"
    else:
        fallback_name, mime = (requested_stem + ".mp4" if requested_stem else "dropfetch_video.mp4"), "video/mp4"

    # Native browser download: do NOT fetch this URL from JavaScript. The
    # frontend opens this URL directly in an <a>, so Chrome owns the download
    # and displays it in the Downloads panel while it is in progress.
    # HTTP headers are Latin-1 encoded by Starlette, so never put the
    # Unicode YouTube title directly into filename="...".
    # Use an ASCII fallback plus RFC 5987 filename* for the real UTF-8 name.
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", fallback_name).strip("._-")
    if not ascii_name:
        ascii_name = "download"

    encoded_name = quote(fallback_name, safe="")

    headers = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{encoded_name}"
        ),
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }

    return StreamingResponse(
        stream_file(temp_dir / "unused", fallback_name, mime),
        media_type=mime,
        headers=headers,
    )


async def stream_direct_source(url: str, temp_dir: Path, state: dict):
    target = temp_dir / "download"
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    filename = None

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "application/octet-stream")
            disposition = r.headers.get("content-disposition", "")
            m = re.search(r'filename="?([^";]+)"?', disposition, flags=re.I)
            if m:
                filename = safe_name(m.group(1))
            if not filename:
                suffix = Path(urlparse(str(r.url)).path).suffix[:10]
                filename = "download" + suffix

            total = 0
            with target.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 128):
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError("File exceeds the server's size limit.")
                    f.write(chunk)

    state["path"] = target
    state["filename"] = filename or "download"


async def stream_image_source(url: str, temp_dir: Path, state: dict):
    opts = base_ydl()
    opts["skip_download"] = True
    info = await run_ydl(url, opts)
    image_url = info.get("thumbnail") or url
    target = temp_dir / "image"

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        async with client.stream("GET", image_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "image/jpeg").split(";")[0].lower()
            if not ctype.startswith("image/"):
                raise RuntimeError("The page did not expose a downloadable image.")
            suffix = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
                "image/avif": ".avif",
            }.get(ctype, ".jpg")
            target = target.with_suffix(suffix)
            total = 0
            with target.open("wb") as f:
                async for chunk in r.aiter_bytes(1024 * 128):
                    total += len(chunk)
                    if total > MAX_FILE_SIZE_MB * 1024 * 1024:
                        raise RuntimeError("Image exceeds the file-size limit.")
                    f.write(chunk)

    state["path"] = target
    state["filename"] = safe_name(info.get("title", "image")) + target.suffix


async def image_download(url: str, temp_dir: Path):
    opts = base_ydl()
    opts["skip_download"] = True
    info = await run_ydl(url, opts)
    image_url = info.get("thumbnail") or url

    target = temp_dir / "image"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        async with client.stream(
            "GET",
            image_url,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            if not ctype.startswith("image/"):
                raise RuntimeError("The page did not expose a downloadable image.")

            suffix = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
                "image/avif": ".avif",
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
        async with client.stream(
            "GET",
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as r:
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
    """Ensure the BgUtils PO-token provider is available.

    In the Render Docker image, the provider is started by Docker CMD before
    Uvicorn. Locally, this FastAPI startup hook starts it when needed.
    """
    global pot_provider_process

    # If the provider was already started by Docker CMD, do not start a
    # second copy on the same port.
    try:
        with socket.create_connection(
            (POT_PROVIDER_HOST, POT_PROVIDER_PORT), timeout=0.25
        ):
            return
    except OSError:
        pass

    provider_candidates = [
        Path("/opt/bgutil/server/build/main.js"),
        Path("/app/build/main.js"),
    ]
    provider = next((p for p in provider_candidates if p.exists()), None)
    node = "/usr/local/bin/node"

    if provider and Path(node).exists():
        pot_provider_process = subprocess.Popen(
            [
                node,
                str(provider),
                "--host",
                POT_PROVIDER_HOST,
                "--port",
                str(POT_PROVIDER_PORT),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )

        # Give the provider a moment to bind its local port.
        for _ in range(20):
            try:
                with socket.create_connection(
                    (POT_PROVIDER_HOST, POT_PROVIDER_PORT), timeout=0.25
                ):
                    break
            except OSError:
                await asyncio.sleep(0.25)


@app.on_event("shutdown")
async def shutdown():
    global pot_provider_process
    if pot_provider_process and pot_provider_process.poll() is None:
        pot_provider_process.terminate()
        try:
            pot_provider_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pot_provider_process.kill()
    pot_provider_process = None

# DropFetch — Final publish-ready starter

DropFetch is a FastAPI + yt-dlp media utility with four modes:
- Video: MP4 with quality selection
- Audio: MP3
- Image: best image/thumbnail exposed by the source
- File: direct HTTP/HTTPS file URL

## Important platform limitation

No public downloader can honestly guarantee every URL from every platform. yt-dlp maintains a large extractor list, but its documentation says sites change and an extractor can stop working. Private/login-only content and DRM are not bypassed.

## Run on Windows

Open PowerShell in this folder:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app:app --reload
```

Open:

http://127.0.0.1:8000

The Docker image includes FFmpeg.

## Docker locally

```powershell
docker build -t dropfetch .
docker run --rm -p 10000:10000 dropfetch
```

Open:

http://127.0.0.1:10000

## Publish with Render

1. Create a GitHub repository named `dropfetch`.
2. Upload every file in this project, including `Dockerfile` and `render.yaml`.
3. Sign in to Render.
4. New -> Web Service.
5. Connect your GitHub repository.
6. Select Docker as the runtime if Render asks.
7. The included `render.yaml` is also provided for configuration.
8. Deploy.
9. Render will provide an `onrender.com` public URL.
10. Test YouTube, Instagram, Pinterest and other supported public URLs.
11. Later, add a custom domain from the Render service settings.

Render web services require the application to listen on 0.0.0.0 and the service port; this Dockerfile uses the `PORT` environment variable and defaults to 10000.

## Why downloads can be slow

The user's browser is not downloading directly from the social platform. Your server first retrieves/processes the media and then sends it to the user. Therefore the server's CPU, RAM, bandwidth, region, source platform and hosting plan affect speed.

The app already uses parallel fragment downloading where supported and avoids storing a permanent media library.

For a high-traffic production service, replace the single request/download flow with:
Browser -> API -> Redis queue -> worker -> object storage -> signed download URL.

That architecture prevents long downloads from tying up web-server workers.

## Production hardening before large public traffic

Add:
- persistent Redis-backed rate limiting
- job queue/background workers
- maximum file size and duration controls
- abuse monitoring
- structured logs
- privacy policy
- terms of service
- copyright/DMCA contact process if applicable
- authentication/admin controls if required
- object storage such as S3/R2 for completed jobs
- automatic cleanup of temporary files
- monitoring/alerts

Do not add mechanisms intended to bypass DRM, private-account access, or platform access controls.

## Updating yt-dlp

yt-dlp changes frequently because websites change. Update regularly:

```powershell
.venv\Scripts\python.exe -m pip install -U "yt-dlp[default]"
```

Then rebuild/redeploy.

## Expected behavior

Video mode:
- inspect public page
- show title/thumbnail
- download MP4

Audio:
- extract audio and return MP3

Image:
- download the image/thumbnail exposed by the extractor

File:
- download a direct public HTTP/HTTPS file URL

A social post containing multiple images may expose only one representative image through the extractor; a future gallery mode would need site-specific handling.

# DropFetch — YouTube PO-Token Edition

DropFetch is a FastAPI + yt-dlp media downloader for publicly accessible media that the user is authorized to download.

## What's new

- Native browser downloads: Chrome receives a normal attachment response instead of a JavaScript Blob download.
- Pinterest and other existing supported downloads remain available.
- YouTube support now includes the official BgUtils Proof-of-Origin token provider integration for yt-dlp.
- Thumbnail proxy and responsive UI remain enabled.

## YouTube note

YouTube increasingly requires Proof-of-Origin tokens for some yt-dlp clients. This build runs the official `bgutil-ytdlp-pot-provider` 2.0.0 provider locally inside the same Docker service and installs the matching yt-dlp plugin. The provider can help with the `Sign in to confirm you're not a bot` error, but it does not guarantee that every YouTube URL will work; YouTube may still reject traffic or require account access for restricted content.

## Deploy

Replace `app.py`, `Dockerfile`, `requirements.txt`, `static/index.html`, and `README.md` in the existing DropFetch repository, then:

```bash
git add .
git commit -m "Add YouTube PO token provider"
git push origin main
```

Render will build the Docker image and deploy the new commit when auto-deploy is enabled.

## Safety / access

Use the service only for media you are authorized to download. This project does not attempt to bypass DRM, private access controls, or paid-content restrictions.

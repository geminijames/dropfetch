# DropFetch — Browser-Native Download Build

DropFetch is a FastAPI + yt-dlp media downloader for publicly accessible supported media URLs.

## What changed in this build

- Download button now starts a **real browser download** instead of using JavaScript `fetch()` and creating a Blob only after the response finishes.
- Chrome receives `Content-Disposition: attachment` immediately, so the file appears in the browser's Downloads panel while the server prepares the media.
- The page no longer shows a fake in-page percentage bar. Chrome owns the actual download progress, like a normal file download.
- Video/audio/file/image modes remain available.
- Thumbnail proxy and favicon improvements are included.
- `/api/health` remains the Render health check endpoint.

## Important behavior

For video/audio extraction, the server may need to download/merge/process the media before bytes can be sent to Chrome. The Chrome download item is created when the response starts, but its byte progress can remain at the starting stage until the generated output is ready. This is normal for the current server-side architecture.

A future high-scale architecture could use a background job queue plus object storage and then give the browser a direct signed download URL.

## Deploy on Render

1. Replace the project files in the GitHub repository with this build.
2. Commit and push to the `main` branch.
3. Render will auto-deploy if Auto Deploy is enabled.
4. Otherwise use **Deploys → Manual Deploy → Deploy latest commit**.
5. Open the public DropFetch URL and hard-refresh the page.
6. Inspect a supported URL, then click **Download**.
7. Open Chrome's download icon in the top-right. The new file should appear there as an active browser download instead of appearing only after the page's JavaScript finishes.

## Usage and limitations

Use the service only for media you are authorized to download. Platform support can change, and login-only/private/DRM-protected media may not work. Do not use the application to bypass access controls or DRM.

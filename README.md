# grabline

A small Django app that downloads **TikTok, Instagram, YouTube and Facebook** videos in
the best available quality (up to 4K on YouTube), without watermarks.

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scriptsctivate
pip install -r requirements.txt
python manage.py runserver
```

Open http://127.0.0.1:8000, paste a link, hit **Grab it**, pick a quality.

No system ffmpeg needed: `imageio-ffmpeg` ships a bundled binary. Node.js
(or Deno) should be on PATH; yt-dlp uses it to solve YouTube's player
challenges.

## How it works

- `POST /api/resolve` runs `yt_dlp.extract_info()` and returns title,
  thumbnail, duration, uploader and the available qualities (a full ladder
  for YouTube, the single best for TikTok/Instagram).
- `GET /api/download?url=<post url>&q=<height>` downloads the post
  server-side with yt-dlp into a temp dir, merges video+audio with ffmpeg
  when the platform splits them (YouTube HD), streams the file back as an
  attachment, then deletes the temp dir.
- **TikTok no-watermark**: yt-dlp asks TikTok's API for the clean render;
  formats flagged `watermarked` are filtered out.
- **Quality**: highest resolution wins; among equal resolutions H.264/AAC is
  preferred for compatibility. Some TikToks are only HD in H.265.
- Only TikTok/Instagram/YouTube/Facebook URLs are accepted, so the server is never an
  open proxy for arbitrary URLs.

## Settings (env vars)

- `GRABLINE_COOKIES`: path to a Netscape `cookies.txt` exported from a
  logged-in browser (default: `./cookies.txt` if present). **Instagram
  currently blocks most anonymous requests**, so you need this for Instagram
  in practice. Use a throwaway account, not your personal one.
- `GRABLINE_MAX_DURATION`: max video length in seconds (default 3600).

Keep yt-dlp updated (`pip install -U "yt-dlp[default]"`); the platforms change
their APIs often and old versions break.

## Notes / things to fix before sharing widely

- **Production settings**: `SECRET_KEY` is hard-coded and `DEBUG = True` in
  `downloader/settings.py`. Move both to env vars before deploying.
- **Downloads are synchronous**: a long YouTube video ties up a worker until
  it is fully downloaded and merged. For real traffic, move downloads to a
  job queue (Celery/RQ) with a progress endpoint.
- **No rate limiting / no auth.** If you deploy this somewhere public, anyone
  with the URL can hammer it. Consider basic auth or an allowlist if you're
  sharing beyond a small group.
- **Legal**: TikTok, Instagram, YouTube and Facebook's Terms of Service all
  prohibit third-party downloading, and redistributing content you don't own
  can raise copyright issues. This is fine as a personal/small-group tool;
  turning it into a public product changes that risk profile.

## Project layout

```
downloader/          # Django project settings/urls
core/
  extractor.py        # yt-dlp wrapper (platform check, resolve, download+merge)
  views.py             # index page + /api/resolve + /api/download
  tests.py             # offline tests (no network)
  urls.py
  templates/core/index.html   # single-page UI
requirements.txt
```

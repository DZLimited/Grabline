# grabline

A small Django app that downloads **TikTok, Instagram, YouTube and Facebook** videos in
the best available quality (up to 4K on YouTube), without watermarks.

## Download for Windows

**[Download Grabline.exe](https://github.com/DZLimited/Grabline/releases/latest/download/Grabline.exe)**
(about 110 MB, Windows 10/11, 64-bit). Nothing else to install.

1. Double-click `Grabline.exe`. It opens in your browser, and the first start can
   take a little while.
2. Paste a video link, click **Grab it**, then choose a quality.
3. Keep the black window open while you download. Close it to stop Grabline.

**"Windows protected your PC"?** Grabline isn't code-signed yet, so Windows warns
about it. Click **More info**, then **Run anyway**. Only do this with a file from
the link above.

Only public posts work. If a platform stops working, download the latest version
from the link above, since these sites change often. Please respect the rights
of whoever made the video.

## Setup for developers

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
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

- `DJANGO_SECRET_KEY`: required in production. The app refuses to start without it.
- `DJANGO_DEBUG`: set to `1` for debug mode. Off by default; `manage.py` turns it
  on for local development.
- `DJANGO_ALLOWED_HOSTS`: comma-separated extra hostnames, for a custom domain.
  Vercel's own hostnames are picked up automatically.
- `GRABLINE_COOKIES`: path to a Netscape `cookies.txt` exported from a
  logged-in browser (default: `./cookies.txt` if present). Instagram blocks
  some anonymous requests, and this gets past that. Use a throwaway account,
  not your personal one.
- `GRABLINE_MAX_DURATION`: max video length in seconds (default 3600, or 300
  on Vercel).

Keep yt-dlp updated (`pip install -U "yt-dlp[default]"`); the platforms change
their APIs often and old versions break.

## Releasing a new Grabline.exe

Push a version tag. GitHub Actions runs the tests, builds the exe on a clean
Windows machine, checks that it starts, and publishes the release:

```bash
git tag v1.0.1
git push origin v1.0.1
```

To build it yourself: `pip install -r requirements-exe.txt`, copy `node.exe` into
`vendor/`, then run `pyinstaller grabline.spec`. The result is `dist/Grabline.exe`.

## Deploying to Vercel

1. Import the GitHub repo as a new Vercel project. Vercel finds Django through
   `manage.py` and installs `requirements.txt`.
2. In the project's environment variables, set `DJANGO_SECRET_KEY` to a new
   value. Generate one with:
   `python -c "import secrets; print(secrets.token_urlsafe(50))"`
3. Deploy, then try a short and a long video from each platform.

Vercel limits to test against (Hobby plan, from its docs):

- Responses are capped at 4.5 MB. The docs don't say whether streamed responses
  are exempt, and `/api/download` streams. If they aren't, larger videos fail.
- A function stops after 300 seconds, on 1 vCPU, so converting VP9/AV1 is the
  slow step to watch.
- Monthly allowances include 4 CPU-hours and 10 GB of origin transfer. Usage
  over an allowance pauses that feature until the 30 days are up.
- Node.js isn't documented as available to Python functions, and yt-dlp may need
  it for YouTube.
- Hobby is for non-commercial, personal use only.

## Notes / things to fix before sharing widely

- **Downloads are synchronous**: a long YouTube video holds its request open until
  it is fully downloaded and merged. For real traffic, move downloads to a
  job queue with a progress endpoint (this needs a host other than Vercel).
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
grabline_app.py      # Grabline.exe entry point (local server + opens browser)
grabline.spec        # PyInstaller build
.github/workflows/release.yml   # builds and publishes the exe on version tags
requirements.txt
requirements-exe.txt # extra packages for building the exe
```

"""
Wraps yt-dlp to resolve and download TikTok / Instagram / YouTube / Facebook posts in
the best available quality, without watermarks.

- TikTok: yt-dlp asks TikTok's own API for the clean (no-watermark) render.
  We additionally filter out any format yt-dlp flags as "watermarked".
- YouTube: anything above 360p is served as separate video + audio streams,
  so we download both and merge them with ffmpeg (bundled via imageio-ffmpeg).
- Instagram: public posts/reels work anonymously most of the time; set
  GRABLINE_COOKIES to a cookies.txt file to get past login walls.
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from django.conf import settings
from yt_dlp.networking.impersonate import ImpersonateTarget

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # noqa: BLE001 - fall back to ffmpeg on PATH
    FFMPEG = None


PLATFORMS = {
    "tiktok": ("tiktok.com",),
    "instagram": ("instagram.com",),
    "youtube": ("youtube.com", "youtu.be"),
    "facebook": ("facebook.com", "fb.watch", "fb.com"),
}

# Never hand out TikTok's watermarked "download" render.
NO_WATERMARK = "[format_note!*=watermarked]"


logger = logging.getLogger(__name__)


TEMP_PREFIX = "grabline-"
# Well past the longest a request can run, so live downloads are never removed.
STALE_TEMP_SECONDS = 60 * 60


class ExtractionError(Exception):
    pass


def detect_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for platform, domains in PLATFORMS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return platform
    raise ExtractionError("That link isn't supported. Paste a TikTok, Instagram, YouTube or Facebook video link.")


def _js_runtimes() -> dict:
    # Grabline.exe ships its own node.exe, since most users won't have Node.
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return {"node": {"path": os.path.join(bundle_dir, "node.exe")}}
    return {"deno": {}, "node": {}}


def _base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        # TikTok fingerprints the TLS client; impersonating Chrome (needs
        # curl_cffi) gets past that.
        "impersonate": ImpersonateTarget.from_str("chrome"),
        # YouTube needs a JS runtime to solve its player challenges.
        "js_runtimes": _js_runtimes(),
        # The default cache lives in the home dir, which is read-only on Vercel.
        "cachedir": os.path.join(tempfile.gettempdir(), "ytdlp-cache"),
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
    cookies = getattr(settings, "GRABLINE_COOKIES", None)
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    return opts


def _format_opts(height: int | None) -> dict:
    """
    Best video + best audio, merged to mp4. Highest resolution wins (capped
    at `height` if given); among equal resolutions prefer H.264/AAC so the
    file plays everywhere (phones, Windows, editors).
    """
    res = f"res:{height}" if height else "res"
    return {
        "format": f"bv*{NO_WATERMARK}+ba/b{NO_WATERMARK}/bv*+ba/b",
        "format_sort": [res, "vcodec:h264", "acodec:aac", "ext"],
        "merge_output_format": "mp4",
    }


PLATFORM_NAMES = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube", "facebook": "Facebook"}

# (substrings to look for in yt-dlp's error, friendly message). First match wins.
# "{p}" is replaced with the platform name. Order matters: specific before generic.
FRIENDLY_ERRORS = [
    (("confirm you're not a bot", "confirm you are not a bot", "http error 429", "too many requests", "rate-limit", "rate limit"),
     "{p} is temporarily limiting our requests. Please try again in a few minutes."),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"),
     "This video is age-restricted on {p}, so it can't be downloaded without signing in."),
    (("is live", "live event", "premieres in", "will begin in", "this live stream"),
     "Live streams and premieres can't be downloaded until they've finished."),
    (("private video", "this video is private", "this account is private", "only available for registered users",
      "login required", "log in", "login", "cookies", "empty media response", "you must be logged in"),
     "This {p} post is private or needs a login to view, so we can't download it. Only public posts work."),
    (("video unavailable", "has been removed", "been deleted", "does not exist", "no longer available",
      "not available in your country", "geo restrict", "http error 404", "page not found", "content isn't available",
      "this content isn't available"),
     "This video is unavailable. It may have been deleted, made private, or blocked in this region."),
    (("no video formats found", "there's no video", "no video in this post", "does not contain a video",
      "no downloadable media", "requested format is not available"),
     "We couldn't find a video in this post. It may be a photo or text post."),
    (("unsupported url", "is not a valid url"),
     "This doesn't look like a {p} video link. Open the post, tap Share, then Copy link, and paste that."),
    (("timed out", "timeout", "connection", "network is unreachable", "temporary failure in name resolution",
      "getaddrinfo", "ssl"),
     "We couldn't reach {p} right now. Please check the link and try again."),
]
GROUP_POST_MSG = ("Facebook group posts usually can't be downloaded unless the group is public. "
                  "Try opening the video itself (tap it, then Share, then Copy link) and paste that link.")
GENERIC_MSG = ("We couldn't download this {p} video. Make sure the post is public and the link is correct, "
               "then try again.")


def _clean_error(e: Exception, url: str = "", platform: str | None = None) -> str:
    """
    Turn a raw yt-dlp error into a message an end user can act on. The raw
    text (with GitHub links, CLI flags etc.) is logged, never shown.
    """
    raw = re.sub(r"\[[0-9;]*m", "", str(e))
    logger.warning("yt-dlp error for %s: %s", url, raw)
    msg = raw.lower()
    p = PLATFORM_NAMES.get(platform or "", "the site")

    for needles, friendly in FRIENDLY_ERRORS:
        if any(n in msg for n in needles):
            return friendly.format(p=p)
    if platform == "facebook" and "/groups/" in url:
        return GROUP_POST_MSG
    return GENERIC_MSG.format(p=p)


def _extract(url: str, opts: dict, platform: str) -> dict:
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info)
    except yt_dlp.utils.DownloadError as e:
        raise ExtractionError(_clean_error(e, url, platform)) from e
    if info is None:
        raise ExtractionError(GENERIC_MSG.format(p=PLATFORM_NAMES[platform]))
    # Single posts sometimes come back playlist-shaped (e.g. IG carousels).
    if "entries" in info:
        entries = [e for e in info["entries"] if e and e.get("formats")]
        if not entries:
            raise ExtractionError("We couldn't find a video in this post. It may be a photo or text post.")
        info = entries[0]
    return info


def _check_duration(info: dict) -> None:
    max_dur = getattr(settings, "GRABLINE_MAX_DURATION", None)
    if max_dur and (info.get("duration") or 0) > max_dur:
        raise ExtractionError(f"This video is too long. We can download videos up to {max_dur // 60} minutes.")


def resolve(url: str) -> dict:
    """Metadata + available quality options for the preview card."""
    platform = detect_platform(url)
    info = _extract(url, {**_base_opts(), **_format_opts(None), "skip_download": True}, platform)

    _check_duration(info)

    # Quality = short side, so a 720x1280 portrait TikTok/Short counts as
    # 720p. This matches how yt-dlp's "res" sort caps resolution.
    heights = sorted(
        {min(f["height"], f.get("width") or f["height"]) for f in info.get("formats") or []
         if f.get("height") and f.get("vcodec") != "none"
         and "watermarked" not in (f.get("format_note") or "")},
        reverse=True,
    )
    # Only YouTube really offers a meaningful quality ladder.
    qualities = [h for h in heights if h >= 360] if platform == "youtube" else heights[:1]

    return {
        "platform": platform,
        "title": _clean_title(info.get("title") or info.get("description")),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "width": info.get("width"),
        "height": info.get("height"),
        "uploader": info.get("uploader") or info.get("channel"),
        "qualities": qualities,
    }


def download(url: str, height: int | None = None) -> tuple[Path, str]:
    """
    Download (and merge, if needed) into a fresh temp dir.
    Returns (file_path, suggested_filename). Caller must delete file_path.parent.
    """
    platform = detect_platform(url)
    _remove_stale_tempdirs()
    tmpdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    opts = {
        **_base_opts(),
        **_format_opts(height),
        "outtmpl": str(tmpdir / "%(id)s.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Read the details first so the length limit applies here too;
            # /api/download can be called without going through /api/resolve.
            info = ydl.extract_info(url, download=False)
            if info:
                _check_duration(next((e for e in info.get("entries") or [] if e), info))
            info = ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as e:
        _rmtree(tmpdir)
        raise ExtractionError(_clean_error(e, url, platform)) from e
    except Exception:
        _rmtree(tmpdir)
        raise

    files = [p for p in tmpdir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        _rmtree(tmpdir)
        raise ExtractionError(GENERIC_MSG.format(p=PLATFORM_NAMES[platform]))
    path = max(files, key=lambda p: p.stat().st_size)
    try:
        path = _make_compatible(path)
    except Exception:
        _rmtree(tmpdir)
        raise

    if info and "entries" in info:
        info = next((e for e in info["entries"] if e), {})
    title = _clean_title((info or {}).get("title"))
    # Dots are replaced too: IG titles are "Video by user.name", which would
    # otherwise look like a double extension ("user.name.mp4").
    title = re.sub(r'[\\/:*?"<>|.\r\n\t]+', "_", title).strip(" _")[:120] or "video"
    return path, f"{title}{path.suffix}"


def _clean_title(title: str | None) -> str:
    # Facebook prefixes titles with stats: "9.9K views · 340 reactions | Real title".
    title = re.sub(r"^[\d.,]+[KMB]?\s+views?\s*·\s*[\d.,]+[KMB]?\s+reactions?\s*\|\s*", "", title or "")
    return title.strip() or "video"


def _probe_codecs(path: Path) -> tuple[str | None, str | None]:
    """(video_codec, audio_codec) as ffmpeg names them, e.g. ("vp9", "aac")."""
    out = subprocess.run(
        [FFMPEG or "ffmpeg", "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stderr
    v = re.search(r"Stream #.*?Video: (\w+)", out)
    a = re.search(r"Stream #.*?Audio: (\w+)", out)
    return (v and v.group(1)), (a and a.group(1))


def _make_compatible(path: Path) -> Path:
    """
    Instagram/YouTube often serve their top resolution as VP9 or AV1 only.
    Those play in browsers but WhatsApp, iPhones and many Android galleries
    reject them, so re-encode anything that isn't H.264/H.265 to H.264 (same
    resolution) and non-AAC audio to AAC. Compatible streams are just copied.
    Always rewrite with +faststart so the file is playable while streaming.
    """
    vcodec, acodec = _probe_codecs(path)
    cmd = [FFMPEG or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
           "-map", "0:v:0?", "-map", "0:a:0?"]
    if vcodec == "h264":
        cmd += ["-c:v", "copy"]
    elif vcodec == "hevc":
        cmd += ["-c:v", "copy", "-tag:v", "hvc1"]  # hvc1 tag is what Apple players require
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy"] if acodec == "aac" else ["-c:a", "aac", "-b:a", "160k"]
    out = path.with_name(path.stem + "_final.mp4")
    cmd += ["-movflags", "+faststart", str(out)]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not out.exists():
        logger.error("ffmpeg failed for %s: %s", path, result.stderr[-2000:])
        raise ExtractionError("We downloaded the video but couldn't prepare it. Please try again.")
    path.unlink(missing_ok=True)
    return out


def _remove_stale_tempdirs() -> None:
    """Clear what a crash or a timed-out request left behind."""
    cutoff = time.time() - STALE_TEMP_SECONDS
    for path in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                _rmtree(path)
        except OSError:
            pass  # another request got to it first


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)

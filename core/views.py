import json
import logging
import re
import shutil

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils.http import content_disposition_header
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .extractor import ExtractionError, detect_platform, download, resolve

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{6,40}$")
CHUNK = 64 * 1024
ERROR_PREFIX = "grabline-error: "


def _valid_url(url: str) -> str | None:
    url = (url or "").strip()
    return url if URL_RE.match(url) else None


@ensure_csrf_cookie
def index(request):
    return render(request, "core/index.html")


@require_POST
def resolve_link(request):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Please paste a valid video link."}, status=400)

    url = _valid_url(body.get("url"))
    if not url:
        return JsonResponse({"error": "Please paste a valid video link."}, status=400)

    try:
        return JsonResponse(resolve(url))
    except ExtractionError as e:
        return JsonResponse({"error": str(e)}, status=422)
    except Exception:  # noqa: BLE001
        logger.exception("resolve failed for %s", url)
        return JsonResponse({"error": "Something went wrong on our side. Please try again in a moment."}, status=500)


def _download_error(message: str, status: int = 400) -> HttpResponse:
    # Rendered inside the page's hidden download frame; the page reads this
    # text and shows it in the status line. The prefix tells our messages apart
    # from error pages the hosting platform may put in the frame instead.
    return HttpResponse(ERROR_PREFIX + message, status=status, content_type="text/plain; charset=utf-8")


class _TempFileStream:
    """Streams a file, then removes its temp dir even if streaming never starts."""

    def __init__(self, path):
        self.path = path

    def __iter__(self):
        with open(self.path, "rb") as f:
            while chunk := f.read(CHUNK):
                yield chunk

    def close(self):
        shutil.rmtree(self.path.parent, ignore_errors=True)


@require_GET
@xframe_options_sameorigin  # errors are read from the page's hidden frame
def download_proxy(request):
    """
    Downloads the post server-side with yt-dlp (merging video+audio when the
    platform splits them, e.g. YouTube HD) and streams the finished file back
    as an attachment. Only accepts supported platform post URLs, never
    arbitrary media URLs, so the server can't be used as an open proxy.
    """
    url = _valid_url(request.GET.get("url"))
    if not url:
        return _download_error("Please paste a valid video link.")
    try:
        detect_platform(url)
    except ExtractionError as e:
        return _download_error(str(e))

    q = request.GET.get("q")
    height = int(q) if q and q.isdigit() else None
    token = request.GET.get("t", "")

    try:
        path, filename = download(url, height)
    except ExtractionError as e:
        return _download_error(str(e), 422)
    except Exception:  # noqa: BLE001
        logger.exception("download failed for %s", url)
        return _download_error("Something went wrong on our side. Please try again in a moment.", 500)

    response = StreamingHttpResponse(_TempFileStream(path), content_type="video/mp4" if path.suffix == ".mp4" else "application/octet-stream")
    response["Content-Disposition"] = content_disposition_header(True, filename)
    response["Content-Length"] = str(path.stat().st_size)
    if TOKEN_RE.match(token):
        # Lets the page know the file has started downloading.
        response.set_cookie("grabline_dl", token, max_age=60, samesite="Lax")
    return response

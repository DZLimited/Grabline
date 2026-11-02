import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.test import Client, TestCase, override_settings

from .extractor import (
    STALE_TEMP_SECONDS, TEMP_PREFIX, ExtractionError, _remove_stale_tempdirs, detect_platform, download,
)
from .views import _TempFileStream


class DetectPlatformTests(TestCase):
    def test_supported(self):
        self.assertEqual(detect_platform("https://www.tiktok.com/@a/video/1"), "tiktok")
        self.assertEqual(detect_platform("https://vm.tiktok.com/ZM123/"), "tiktok")
        self.assertEqual(detect_platform("https://www.instagram.com/reel/abc/"), "instagram")
        self.assertEqual(detect_platform("https://youtu.be/abc"), "youtube")
        self.assertEqual(detect_platform("https://www.facebook.com/watch?v=1"), "facebook")
        self.assertEqual(detect_platform("https://m.facebook.com/reel/1"), "facebook")
        self.assertEqual(detect_platform("https://fb.watch/abc/"), "facebook")
        self.assertEqual(detect_platform("https://m.youtube.com/shorts/abc"), "youtube")

    def test_rejected(self):
        for url in ["https://vimeo.com/x", "https://eviltiktok.com/x", "https://notfacebook.com/x",
                    "http://127.0.0.1/admin", "https://youtube.com.evil.io/x"]:
            with self.assertRaises(ExtractionError):
                detect_platform(url)


class ViewTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.get("/")
        self.token = self.client.cookies["csrftoken"].value

    def post(self, url, token=True):
        headers = {"HTTP_X_CSRFTOKEN": self.token} if token else {}
        return self.client.post("/api/resolve", json.dumps({"url": url}),
                                content_type="application/json", **headers)

    def test_resolve_requires_csrf(self):
        self.assertEqual(self.post("https://youtu.be/x", token=False).status_code, 403)

    def test_resolve_rejects_bad_url(self):
        self.assertEqual(self.post("not a url").status_code, 400)

    def test_resolve_rejects_unsupported_platform(self):
        self.assertEqual(self.post("https://vimeo.com/1").status_code, 422)

    @mock.patch("core.views.resolve", return_value={"platform": "youtube", "qualities": [1080]})
    def test_resolve_ok(self, _):
        r = self.post("https://youtu.be/x")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["qualities"], [1080])

    def test_download_is_not_an_open_proxy(self):
        r = self.client.get("/api/download", {"url": "http://169.254.169.254/latest/meta-data"})
        self.assertEqual(r.status_code, 400)


class FriendlyErrorTests(TestCase):
    """Raw yt-dlp errors must never reach users (no GitHub links, CLI flags)."""

    def check(self, raw, url="https://www.facebook.com/watch?v=1", platform="facebook"):
        from .extractor import _clean_error
        msg = _clean_error(Exception(raw), url, platform)
        for leak in ("github", "yt-dlp", "--cookies", "ERROR:", "report this issue"):
            self.assertNotIn(leak.lower(), msg.lower())
        return msg

    def test_facebook_group_parse_error(self):
        msg = self.check("ERROR: [facebook] 186: Cannot parse data; please report this issue on "
                         "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue "
                         "template. Confirm you are on the latest version using yt-dlp -U",
                         url="https://www.facebook.com/groups/240912277321716/permalink/186/")
        self.assertIn("group", msg)

    def test_instagram_login_wall(self):
        msg = self.check("ERROR: [Instagram] X: Instagram sent an empty media response. Check if this post "
                         "is accessible in your browser without being logged-in. If it is not, then use "
                         "--cookies-from-browser or --cookies for the authentication.",
                         url="https://www.instagram.com/reel/X/", platform="instagram")
        self.assertIn("private", msg)

    def test_youtube_cases(self):
        yt = {"url": "https://youtu.be/x", "platform": "youtube"}
        self.assertIn("temporarily", self.check("ERROR: [youtube] x: Sign in to confirm you're not a bot. "
                                                "Use --cookies-from-browser", **yt))
        self.assertIn("age-restricted", self.check("ERROR: [youtube] x: Sign in to confirm your age.", **yt))
        self.assertIn("unavailable", self.check("ERROR: [youtube] x: Video unavailable", **yt))

    def test_id_digits_do_not_trigger_rate_limit(self):
        msg = self.check("ERROR: [facebook] 1195289147629429: Something odd happened")
        self.assertNotIn("limiting", msg)

    def test_unknown_error_is_generic(self):
        self.assertIn("couldn't download", self.check("ERROR: [facebook] 1: KeyError('weird')"))


class DownloadErrorTests(TestCase):
    @mock.patch("core.views.download", side_effect=ExtractionError("This video is unavailable."))
    def test_download_error_is_plain_text(self, _):
        r = Client().get("/api/download", {"url": "https://youtu.be/x"})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.content.decode(), "grabline-error: This video is unavailable.")
        self.assertTrue(r["Content-Type"].startswith("text/plain"))

    def test_download_error_can_load_in_the_hidden_frame(self):
        r = Client().get("/api/download", {"url": "https://vimeo.com/1"})
        self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN")


class DownloadLimitTests(TestCase):
    @override_settings(GRABLINE_MAX_DURATION=60)
    def test_long_video_is_refused_before_downloading(self):
        ydl = mock.MagicMock()
        ydl.extract_info.return_value = {"duration": 61}
        with mock.patch("core.extractor.yt_dlp.YoutubeDL") as cls:
            cls.return_value.__enter__.return_value = ydl
            with self.assertRaisesMessage(ExtractionError, "too long"):
                download("https://youtu.be/x")
        ydl.process_ie_result.assert_not_called()


class TempFileTests(TestCase):
    def make_dir(self, prefix=TEMP_PREFIX):
        path = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_close_removes_files_when_streaming_never_started(self):
        folder = self.make_dir()
        video = folder / "v.mp4"
        video.write_bytes(b"data")
        _TempFileStream(video).close()
        self.assertFalse(folder.exists())

    def test_stale_dirs_are_removed_and_recent_ones_kept(self):
        stale, recent = self.make_dir(), self.make_dir()
        old = time.time() - STALE_TEMP_SECONDS - 60
        os.utime(stale, (old, old))
        _remove_stale_tempdirs()
        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())

import json
from unittest import mock

from django.test import Client, TestCase

from .extractor import ExtractionError, detect_platform


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
        self.assertEqual(r.content.decode(), "This video is unavailable.")
        self.assertTrue(r["Content-Type"].startswith("text/plain"))

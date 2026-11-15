import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import flask  # noqa: F401
    import psycopg2  # noqa: F401
except ImportError:  # the web service needs Flask and a DB driver
    flask = None

requires_web_deps = unittest.skipIf(flask is None, "Flask or psycopg2 is not installed")


def load_web_service():
    # web_service.init_db() connects to PostgreSQL at import time; stub it out.
    with mock.patch("psycopg2.connect"):
        import web_service
    return web_service


@requires_web_deps
class UploadVideoUrlTests(unittest.TestCase):
    def setUp(self):
        self.web_service = load_web_service()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [
            mock.patch.object(self.web_service, "UPLOAD_DIR", Path(self.tmp.name)),
            mock.patch.object(self.web_service, "get_db", return_value=mock.MagicMock()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.client = self.web_service.app.test_client()

    def upload(self, company_name, filename="clip.webm"):
        return self.client.post(
            "/upload",
            data={"company_name": company_name, "file": (io.BytesIO(b"data"), filename)},
            content_type="multipart/form-data",
        )

    def test_video_url_is_percent_encoded(self):
        response = self.upload("nutrimmun GmbH")
        self.assertEqual(response.status_code, 200)
        video_url = response.get_json()["video_url"]
        self.assertTrue(video_url.endswith("/media/nutrimmun%20GmbH.webm"), video_url)
        self.assertNotIn(" ", video_url)

    def test_non_ascii_company_name_is_encoded(self):
        response = self.upload("Müller Käse", "clip.mp4")
        self.assertEqual(response.status_code, 200)
        video_url = response.get_json()["video_url"]
        self.assertTrue(video_url.endswith("/media/M%C3%BCller%20K%C3%A4se.mp4"), video_url)

    def test_encoded_url_resolves_to_saved_file(self):
        response = self.upload("nutrimmun GmbH")
        path = "/media/" + response.get_json()["video_url"].split("/media/", 1)[1]
        media = self.client.get(path)
        self.assertEqual(media.status_code, 200)
        self.assertEqual(media.data, b"data")
        media.close()


if __name__ == "__main__":
    unittest.main()

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


@requires_web_deps
class ParseCompaniesCsvTests(unittest.TestCase):
    def setUp(self):
        self.web_service = load_web_service()

    def parse(self, text):
        return self.web_service._parse_companies_csv(text.encode("utf-8"))

    def test_valid_csv(self):
        rows = self.parse(
            "URL;Company Name;Language;Country;Postcode;City;Street;HouseNo;Web\n"
            "https://a.de;ACME GmbH;German;Germany;;;;;https://a.de\n"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company_name"], "ACME GmbH")
        self.assertEqual(rows[0]["web"], "https://a.de")
        self.assertIsNone(rows[0]["postcode"])

    def test_bom_and_spaces_in_header(self):
        rows = self.parse("﻿URL; Company Name ;Web\nhttps://a.de;ACME GmbH;a.de\n")
        self.assertEqual(rows[0]["company_name"], "ACME GmbH")
        self.assertEqual(rows[0]["web"], "a.de")

    def test_header_only_file_is_valid_and_empty(self):
        self.assertEqual(self.parse("URL;Company Name;Web\n"), [])

    def test_comma_delimited_file_is_rejected(self):
        with self.assertRaises(self.web_service.csv.Error) as ctx:
            self.parse("URL,Company Name,Web\nhttps://a.de,ACME GmbH,a.de\n")
        self.assertIn("Company Name", str(ctx.exception))

    def test_missing_company_name_column_is_rejected(self):
        with self.assertRaises(self.web_service.csv.Error):
            self.parse("URL;Name;Web\nhttps://a.de;ACME GmbH;a.de\n")

    def test_empty_file_is_rejected(self):
        for text in ("", "\n", "   \n"):
            with self.subTest(text=text):
                with self.assertRaises(self.web_service.csv.Error):
                    self.parse(text)


@requires_web_deps
class UploadCompaniesEndpointTests(unittest.TestCase):
    def setUp(self):
        self.web_service = load_web_service()
        self.get_db = mock.patch.object(
            self.web_service, "get_db", return_value=mock.MagicMock()
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.client = self.web_service.app.test_client()

    def post_csv(self, text):
        return self.client.post(
            "/upload_companies",
            data={"file": (io.BytesIO(text.encode("utf-8")), "companies.csv")},
            content_type="multipart/form-data",
        )

    def test_wrong_delimiter_returns_400_and_does_not_touch_db(self):
        response = self.post_csv("URL,Company Name,Web\nhttps://a.de,ACME GmbH,a.de\n")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Company Name", response.get_json()["error"])
        self.get_db.assert_not_called()

    def test_missing_company_name_column_returns_400(self):
        response = self.post_csv("URL;Name;Web\nhttps://a.de;ACME GmbH;a.de\n")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.get_json()["error"].startswith("Invalid CSV"))

    def test_valid_file_is_imported(self):
        with mock.patch.object(
            self.web_service, "_upsert_company", side_effect=["imported", "updated"]
        ):
            response = self.post_csv(
                "URL;Company Name;Web\n"
                "https://a.de;ACME GmbH;a.de\n"
                "https://b.de;Beta GmbH;b.de\n"
                "https://c.de;;c.de\n"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(), {"imported": 1, "updated": 1, "total_rows": 3}
        )


if __name__ == "__main__":
    unittest.main()

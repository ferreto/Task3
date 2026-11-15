import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import psycopg2  # noqa: F401
except ImportError:  # enrichment script needs a DB driver; unit tests for it are skipped
    psycopg2 = None

requires_psycopg2 = unittest.skipIf(psycopg2 is None, "psycopg2 is not installed")


@requires_psycopg2
class DotenvLoadOrderTests(unittest.TestCase):
    def test_env_file_applies_to_impressum_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("enrich_companies.py", "impressum.py", "company_status.py"):
                shutil.copy(ROOT / name, tmp)
            Path(tmp, ".env").write_text(
                "MAX_TEXT_CHARS=1234\nREQUEST_TIMEOUT=7\nUSER_AGENT=test-agent\n",
                encoding="utf-8",
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in ("MAX_TEXT_CHARS", "REQUEST_TIMEOUT", "USER_AGENT")
            }
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import enrich_companies, impressum; "
                "print(impressum.MAX_TEXT_CHARS, impressum.REQUEST_TIMEOUT, impressum.USER_AGENT)"
            )
            result = subprocess.run(
                [sys.executable, "-c", code, tmp],
                env=env,
                cwd=tmp,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1234 7.0 test-agent")


@requires_psycopg2
class SelectCompaniesTests(unittest.TestCase):
    def run_select(self, **overrides):
        import argparse

        import enrich_companies

        args = argparse.Namespace(company=None, status=None, limit=None)
        for key, value in overrides.items():
            setattr(args, key, value)
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []
        enrich_companies.select_companies(conn, args)
        query, params = cur.execute.call_args[0]
        return " ".join(query.split()), list(params)

    def test_default_selection_skips_ok_rows(self):
        query, params = self.run_select()
        self.assertIn("WHERE (status IS NULL OR status <> 0)", query)
        self.assertEqual(params, [])

    def test_pending_first_then_least_recently_processed(self):
        query, _ = self.run_select(limit=2)
        self.assertIn("ORDER BY (status IS NOT NULL), updated_at, id", query)
        self.assertTrue(query.endswith("LIMIT %s"))

    def test_limit_is_passed_as_parameter(self):
        _, params = self.run_select(limit=2)
        self.assertEqual(params, [2])

    def test_company_filter_ignores_status_default(self):
        query, params = self.run_select(company="ACME GmbH")
        self.assertIn("company_name = %s", query)
        self.assertNotIn("status <> 0", query)
        self.assertEqual(params, ["ACME GmbH"])


@requires_psycopg2
class ProcessCompanyTests(unittest.TestCase):
    HOME_HTML = '<html><body><a href="/impressum">Impressum</a></body></html>'
    ROW = {"id": 1, "company_name": "ACME GmbH", "web": "example.com"}

    def setUp(self):
        import enrich_companies

        self.module = enrich_companies
        self.conn = mock.MagicMock()
        self.logger = logging.getLogger("test_enrich_companies")
        patches = {
            "sleep": mock.patch.object(enrich_companies.time, "sleep"),
            "update_status": mock.patch.object(enrich_companies, "update_status"),
            "update_enriched": mock.patch.object(enrich_companies, "update_enriched"),
            "call_deepseek": mock.patch.object(enrich_companies, "call_deepseek"),
        }
        self.mocks = {}
        for name, patch in patches.items():
            self.mocks[name] = patch.start()
            self.addCleanup(patch.stop)

    def process(self, impressum_html):
        fetch = mock.patch.object(
            self.module,
            "fetch_html",
            side_effect=[
                ("https://example.com/", self.HOME_HTML),
                ("https://example.com/impressum", impressum_html),
            ],
        )
        with fetch:
            return self.module.process_company(self.conn, self.ROW, "key", "model", self.logger)

    def test_empty_page_text_skips_deepseek(self):
        status = self.process("<html><body><script>x()</script></body></html>")
        self.assertEqual(status, self.module.CompanyStatus.PARSING_FAILED)
        self.mocks["call_deepseek"].assert_not_called()
        self.mocks["update_enriched"].assert_not_called()
        self.mocks["update_status"].assert_called_once_with(
            self.conn,
            1,
            self.module.CompanyStatus.PARSING_FAILED,
            impressum_url="https://example.com/impressum",
        )

    def test_boilerplate_only_page_skips_deepseek(self):
        status = self.process("<html><body><p>Datenschutz</p><p>Cookie-Hinweis</p></body></html>")
        self.assertEqual(status, self.module.CompanyStatus.PARSING_FAILED)
        self.mocks["call_deepseek"].assert_not_called()

    def test_page_with_content_calls_deepseek(self):
        self.mocks["call_deepseek"].return_value = {
            "company_name": "ACME GmbH",
            "postal_address": {
                "street": "Hauptstraße",
                "house_no": "1",
                "postcode": "10115",
                "city": "Berlin",
                "country": None,
            },
            "handelsregister": None,
            "vat_number": None,
            "phone": None,
            "email": None,
            "responsible_person": None,
        }
        status = self.process(
            "<html><body><h1>Impressum</h1><p>ACME GmbH, Hauptstraße 1, 10115 Berlin</p></body></html>"
        )
        self.assertEqual(status, self.module.CompanyStatus.OK)
        self.mocks["call_deepseek"].assert_called_once()
        self.mocks["update_enriched"].assert_called_once()


if __name__ == "__main__":
    unittest.main()

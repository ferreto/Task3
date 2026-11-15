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


if __name__ == "__main__":
    unittest.main()

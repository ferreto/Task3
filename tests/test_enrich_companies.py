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


if __name__ == "__main__":
    unittest.main()

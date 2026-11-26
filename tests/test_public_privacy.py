"""Generic public-tree checks; the private research denylist stays local only."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicTreePrivacyTests(unittest.TestCase):
    def test_no_private_paths_secrets_or_runtime_artifacts(self):
        # A filesystem path may start at a quote or whitespace. A URL path
        # such as ncbi.nlm.nih.gov/home/ must not trigger this check.
        home_prefix = re.compile(r"(?<![A-Za-z0-9])/" + "home" + "/")
        key_header = "-----BEGIN " + "PRIVATE KEY-----"
        problems = []
        # Audit reports and the local Git directory are not packaged code.
        # References to the historical project name are legitimate migration
        # documentation; private absolute paths and actual credentials are not.
        for base in ("src", "deploy", "docs", "examples"):
            for path in (ROOT / base).rglob("*"):
                relative = path.relative_to(ROOT).as_posix()
                if "__pycache__" in path.parts:
                    continue
                if path.is_symlink():
                    problems.append((relative, "symlink"))
                    continue
                if not path.is_file():
                    continue
                if path.suffix in {".sqlite", ".sqlite3", ".db", ".log", ".whl", ".pyc"} or path.name.startswith(".env"):
                    problems.append((relative, "runtime_artifact"))
                    continue
                content = path.read_text(encoding="utf-8")
                if home_prefix.search(content) or key_header in content:
                    problems.append((relative, "private_path_or_key"))
                if re.search(r"10\.(?!0000\b)\d{4,9}/\S+", content):
                    problems.append((relative, "unexpected_doi"))
        self.assertEqual(problems, [])

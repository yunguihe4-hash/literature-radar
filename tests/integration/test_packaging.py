"""Offline wheel packaging regression for canonical schema resources."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]


class PackagingTests(unittest.TestCase):
    def test_offline_wheel_contains_every_schema_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            shutil.copytree(
                ROOT, source,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "*.egg-info", ".git"),
            )
            dist = base / "dist"
            env = {**os.environ, "PIP_NO_INDEX": "1", "PYTHONDONTWRITEBYTECODE": "1"}
            completed = subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--no-isolation",
                 "--outdir", str(dist), str(source)],
                env=env, text=True, capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-1500:])
            wheels = list(dist.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with ZipFile(wheels[0]) as archive:
                resources = sorted(
                    name for name in archive.namelist()
                    if name.startswith("literature_radar/schemas/") and name.endswith(".schema.json")
                )
            self.assertEqual(len(resources), 9)
            self.assertEqual(len({Path(name).name for name in resources}), 9)
            self.assertIn("literature_radar/schemas/feedback.schema.json", resources)

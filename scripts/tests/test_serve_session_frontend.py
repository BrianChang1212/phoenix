import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "serve-session-frontend.py"


class FrontendArtifactCommandTests(unittest.TestCase):
    def test_rejects_absolute_asset_even_inside_static_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static" / ".vite").mkdir(parents=True)
            entry = root / "static" / "entry.js"
            entry.write_text("export {};", encoding="utf-8")
            (root / "static" / ".vite" / "manifest.json").write_text(
                json.dumps({"index.tsx": {"file": str(entry)}}), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--server-dir", str(root), "--check"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)

    def test_rejects_missing_or_escaping_assets(self):
        for asset in ("missing.js", "../outside.js"):
            with self.subTest(asset=asset), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "static" / ".vite").mkdir(parents=True)
                (root / "outside.js").write_text("export {};", encoding="utf-8")
                (root / "static" / ".vite" / "manifest.json").write_text(
                    json.dumps({"index.tsx": {"file": asset}}), encoding="utf-8"
                )
                result = subprocess.run(
                    [sys.executable, str(RUNNER), "--server-dir", str(root), "--check"],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_forwards_cli_in_same_process_and_preserves_installed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static" / ".vite").mkdir(parents=True)
            (root / "static" / "entry.js").write_text("export {};", encoding="utf-8")
            (root / "static" / ".vite" / "manifest.json").write_text(
                json.dumps({"index.tsx": {"file": "entry.js"}}), encoding="utf-8"
            )
            package = root / "backend" / "phoenix"
            (package / "server").mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "config.py").write_text('SERVER_DIR = "installed"', encoding="utf-8")
            (package / "server" / "__init__.py").write_text("", encoding="utf-8")
            (package / "server" / "app.py").write_text(
                'from phoenix.config import SERVER_DIR\nTEMPLATES_DIR = SERVER_DIR', encoding="utf-8"
            )
            (package / "server" / "main.py").write_text(
                'import json, sys\nfrom phoenix import config\nfrom phoenix.server import app\n'
                'def main():\n print(json.dumps({"static_root": str(app.SERVER_DIR), '
                '"templates": app.TEMPLATES_DIR, "config": config.SERVER_DIR, "argv": sys.argv}))',
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--server-dir", str(root), "--", "serve", "--port", "6007"],
                env={**os.environ, "PYTHONPATH": str(root / "backend")},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(Path(report["static_root"]), root.resolve())
            self.assertEqual(report["config"], "installed")
            self.assertEqual(report["templates"], "installed")
            self.assertEqual(report["argv"], ["phoenix", "serve", "--port", "6007"])

    def test_rejects_artifact_without_executable_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static" / ".vite").mkdir(parents=True)
            (root / "static" / "entry.css").write_text("body{}", encoding="utf-8")
            (root / "static" / ".vite" / "manifest.json").write_text(
                json.dumps({"index.tsx": {"file": "entry.css"}}), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--server-dir", str(root), "--check"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("entry", result.stderr)

    def test_check_reports_complete_artifact_without_importing_phoenix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "static" / ".vite").mkdir(parents=True)
            (root / "static" / "assets").mkdir()
            (root / "static" / "assets" / "entry.js").write_text("export {};", encoding="utf-8")
            (root / "static" / "assets" / "entry.css").write_text("body{}", encoding="utf-8")
            (root / "static" / ".vite" / "manifest.json").write_text(
                json.dumps({"index.tsx": {"file": "assets/entry.js", "isEntry": True,
                                          "css": ["assets/entry.css"]}}), encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(RUNNER), "--server-dir", str(root), "--check"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "artifact-valid")
            self.assertEqual(report["asset_count"], 2)
            self.assertEqual(len(report["manifest_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()

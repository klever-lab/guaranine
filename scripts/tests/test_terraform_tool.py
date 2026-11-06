"""Regressions for the shared worker/CI Terraform tool bootstrap."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terraform_tool import ensure_terraform


class TerraformToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("terraform", b"verified binary fixture")
            archive.writestr("unexpected-file", b"must not be extracted")
        self.archive = buffer.getvalue()
        self.manifest = self.root / "checksums.json"
        self.manifest.write_text(json.dumps({
            "version": "1.17.1",
            "sha256": {"linux_amd64": hashlib.sha256(self.archive).hexdigest()},
        }))
        for name, value in (
            ("CHECKSUMS_PATH", self.manifest),
            ("platform.system", "Linux"),
            ("platform.machine", "x86_64"),
            ("shutil.which", None),
        ):
            arguments = {"new": value} if name == "CHECKSUMS_PATH" else {"return_value": value}
            patcher = patch("terraform_tool." + name, **arguments)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missing_cli_downloads_only_the_verified_binary(self):
        with patch("terraform_tool.urllib.request.urlopen", return_value=io.BytesIO(self.archive)) as download:
            binary = ensure_terraform("1.17.1", self.root, {})
        self.assertEqual(binary.read_bytes(), b"verified binary fixture")
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.root / "unexpected-file").exists())
        download.assert_called_once_with(
            "https://releases.hashicorp.com/terraform/1.17.1/terraform_1.17.1_linux_amd64.zip", timeout=60,
        )

    def test_checksum_failure_never_installs_the_download(self):
        with patch("terraform_tool.urllib.request.urlopen", return_value=io.BytesIO(b"tampered archive")):
            with self.assertRaisesRegex(SystemExit, "SHA-256 mismatch"):
                ensure_terraform("1.17.1", self.root, {})
        self.assertFalse((self.root / "terraform").exists())

    def test_matching_installed_cli_requires_no_download(self):
        with patch("terraform_tool.shutil.which", return_value="/installed/terraform"):
            with patch("terraform_tool.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, '{"terraform_version":"1.17.1"}',
            )):
                with patch("terraform_tool.urllib.request.urlopen") as download:
                    self.assertEqual(ensure_terraform("1.17.1", self.root, {}), Path("/installed/terraform"))
                    download.assert_not_called()

    def test_different_installed_version_gets_the_pinned_cli(self):
        with patch("terraform_tool.shutil.which", return_value="/installed/terraform"):
            with patch("terraform_tool.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, '{"terraform_version":"0.0.0"}',
            )):
                with patch("terraform_tool.urllib.request.urlopen", return_value=io.BytesIO(self.archive)):
                    self.assertEqual(ensure_terraform("1.17.1", self.root, {}), self.root / "terraform")

    def test_pin_changes_require_updated_checksums(self):
        with self.assertRaisesRegex(SystemExit, "match .terraform-version"):
            ensure_terraform("1.17.2", self.root, {})

    def test_unsupported_platform_does_not_download(self):
        with patch("terraform_tool.platform.machine", return_value="unknown"):
            with patch("terraform_tool.urllib.request.urlopen") as download:
                with self.assertRaisesRegex(SystemExit, "supports Linux/macOS"):
                    ensure_terraform("1.17.1", self.root, {})
                download.assert_not_called()

    def test_download_failure_does_not_install_a_binary(self):
        with patch("terraform_tool.urllib.request.urlopen", side_effect=urllib.error.URLError("unavailable")):
            with self.assertRaisesRegex(SystemExit, "Cannot download pinned Terraform"):
                ensure_terraform("1.17.1", self.root, {})
        self.assertFalse((self.root / "terraform").exists())


if __name__ == "__main__":
    unittest.main()

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


finalize = module("iso_finalize", "scripts/iso/finalize.py")
builder = module("iso_builder", "images/laptop/build.py")


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        image = self.directory / "guaranine-worker-amd64.iso"
        image.write_bytes(b"DISPOSABLE IMAGE FIXTURE, NOT AN INSTALLER")
        self.manifest = {"source_commit": "a" * 40, "source_dirty": False, "iso_sha256": finalize.digest(image), "qemu_tests": "pending"}
        result = {**{key: "passed" for key in finalize.REQUIRED}, "installed_packages": ["fixture=1"]}
        self.tests = {"source_commit": "a" * 40, "iso_sha256": finalize.digest(image), "unique_identities": "passed",
                      "firmware": {"bios": copy.deepcopy(result), "uefi": copy.deepcopy(result)}}
        self.write()

    def write(self):
        (self.directory / "build.json").write_text(json.dumps(self.manifest))
        (self.directory / "qemu-results.json").write_text(json.dumps(self.tests))

    def test_finalization_binds_results_and_checksums(self):
        finalize.finalize(self.directory)
        manifest = json.loads((self.directory / "build.json").read_text())
        self.assertEqual(manifest["qemu_tests"], self.tests)
        for line in (self.directory / "SHA256SUMS").read_text().splitlines():
            digest, filename = line.split()
            self.assertEqual(digest, finalize.digest(self.directory / filename))

    def test_package_manifest_removes_terminal_controls_without_losing_rows(self):
        captured = '\x1b[?2004l\r\r\nansible-core=2.19.11-0+deb13u1\r\r\n\r\nlibapt-pkg7.0=3.0.3\r\r\n\x1b[?2004h'
        self.assertEqual(finalize.package_records(captured), ['ansible-core=2.19.11-0+deb13u1', 'libapt-pkg7.0=3.0.3'])
        with self.assertRaises(ValueError):
            finalize.package_records('ansible-core=2.19.11\nUnexpected command output')

    def test_missing_skipped_or_failed_tests_cannot_finalize(self):
        for mode in ("bios", "uefi"):
            for key in finalize.REQUIRED:
                with self.subTest(mode=mode, key=key):
                    saved = self.tests["firmware"][mode].pop(key)
                    self.write()
                    with self.assertRaises((AssertionError, KeyError)):
                        finalize.finalize(self.directory)
                    self.tests["firmware"][mode][key] = saved
        self.tests["firmware"]["uefi"]["automatic_enrollment"] = "skipped"
        self.write()
        with self.assertRaises(AssertionError):
            finalize.finalize(self.directory)

    def test_foreign_image_commit_and_dirty_source_rejected(self):
        self.tests["source_commit"] = "b" * 40
        self.write()
        with self.assertRaises(AssertionError):
            finalize.validate(self.directory)
        self.tests["source_commit"] = "a" * 40
        self.manifest["source_dirty"] = True
        self.write()
        with self.assertRaises(AssertionError):
            finalize.validate(self.directory, require_clean=True)
        self.manifest["source_dirty"] = False
        self.write()
        (self.directory / "guaranine-worker-amd64.iso").write_bytes(b"DIFFERENT IMAGE")
        with self.assertRaises(AssertionError):
            finalize.validate(self.directory)

    def test_download_checksum_failure_cannot_enter_build(self):
        class Response:
            def __enter__(self):
                import io
                return io.BytesIO(b"CORRUPT DOWNLOAD")
            def __exit__(self, *args):
                pass
        spec = {"url": "https://invalid.test/public-tool", "sha256": hashlib.sha256(b"EXPECTED").hexdigest()}
        destination = self.directory / "tool"
        with patch.dict("os.environ", {"GUARANINE_DOWNLOAD_CACHE": str(self.directory)}), patch.object(builder.urllib.request, "urlopen", return_value=Response()), patch.object(builder.time, "sleep"):
            with self.assertRaises(ValueError):
                builder.fetch(spec, destination)
        self.assertFalse(destination.exists())

    def test_workflow_cannot_publish_failed_or_untrusted_previews(self):
        path = ROOT / ".github/workflows/laptop-iso.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertNotIn("pull_request_target", workflow["on"])
        self.assertIn("workflow_dispatch", workflow["on"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        release = workflow["jobs"]["release"]
        self.assertEqual(set(release["needs"]), {"trust", "build-test"})
        self.assertIn("github.event_name == 'push'", release["if"])
        self.assertIn("refs/tags/worker-iso-v", release["if"])
        for name, job in workflow["jobs"].items():
            self.assertIn("timeout-minutes", job)
            if name != "release":
                self.assertNotIn("write", job.get("permissions", {}).values())
            for step in job["steps"]:
                self.assertNotIn("continue-on-error", step)
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[a-f0-9]{40}$")
        run_commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["build-test"]["steps"])
        self.assertIn("bash scripts/validate.sh", run_commands)
        self.assertIn("scripts/iso/qemu-test.py", run_commands)
        self.assertIn("scripts/iso/finalize.py", run_commands)
        guard = (ROOT / "scripts/iso/check-release-ref.sh").read_text()
        self.assertIn("merge-base --is-ancestor", guard)
        self.assertIn(".merged_at != null", guard)
        self.assertNotIn('"APPROVED"', guard)
        self.assertIn('passed("build-test")', guard)
        publisher = (ROOT / "scripts/iso/publish-release.py").read_text()
        self.assertNotIn('"--clobber"', publisher)
        self.assertIn("Versioned asset differs", publisher)


if __name__ == "__main__":
    unittest.main()

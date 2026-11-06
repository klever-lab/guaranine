import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("worker", ROOT / "provision/worker/files/worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def ca_pem():
    bundle = Path(ssl.get_default_verify_paths().cafile).read_text()
    return re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\n", bundle, re.S)[0]


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, value in (("STATE", self.root), ("CONFIG", self.root / "enrollment.json"), ("PROFILE", ROOT / "images/laptop"), ("RUNTIME", self.root / "run")):
            change = patch.object(worker, name, value)
            change.start()
            self.addCleanup(change.stop)
        ca = ca_pem()
        self.config = {"server": "https://100.64.0.1:6443", "server_ca": ca,
                       "bootstrap_token": "K10" + hashlib.sha256(ca.encode()).hexdigest() + "::abcdef.0123456789abcdef",
                       "tailscale_auth_key": "DISPOSABLE-TEST-KEY", "verification_namespace": "enroll-test",
                       "verification_token": "DISPOSABLE-TEST-TOKEN"}

    def node(self):
        return {"metadata": {"name": worker.identity(), "uid": "test-node-uid"}, "spec": {}, "status": {
            "conditions": [{"type": "Ready", "status": "True"}], "addresses": [{"type": "InternalIP", "address": "100.64.0.2"}],
            "allocatable": {"cpu": "1800m", "memory": "1500Mi", "ephemeral-storage": "15000000Ki"}}}

    def test_unique_identity_is_stable_across_retries(self):
        first = worker.identity()
        self.assertEqual(first, worker.identity())
        (self.root / "node-name").unlink()
        self.assertNotEqual(first, worker.identity())

    def test_configuration_accepts_only_trusted_tailnet_bootstrap(self):
        worker.validate_config(self.config)
        for server in ("https://192.168.1.1:6443", "https://1.1.1.1:6443", "http://100.64.0.1:6443", "https://100.64.0.1:443", "https://user:pass@100.64.0.1:6443", "https://100.64.0.1:6443/x"):
            with self.subTest(server=server), self.assertRaises(worker.Blocker):
                worker.validate_config({**self.config, "server": server})
        for token in ("server:administrator", "node:shared-agent-key", "abcdef.0123456789abcdef", "K10" + "0" * 64 + "::abcdef.0123456789abcdef"):
            with self.subTest(token=token), self.assertRaises(worker.Blocker):
                worker.validate_config({**self.config, "bootstrap_token": token})

    def test_protected_files_and_secret_free_failure(self):
        worker.write_json(worker.CONFIG, self.config)
        self.assertEqual(worker.CONFIG.stat().st_mode & 0o777, 0o600)
        worker.status("blocked", "Credentials rejected")
        content = (self.root / "status.json").read_text()
        self.assertNotIn("DISPOSABLE", content)
        self.assertFalse(json.loads(content)["success"])
        worker.CONFIG.chmod(0o644)
        with self.assertRaises(worker.Blocker):
            worker.load_config()

    def test_console_cannot_fall_back_to_echoing_credentials(self):
        def unprotected(label):
            import warnings
            warnings.warn("No protected terminal", worker.getpass.GetPassWarning)
            self.fail("Must not reach plaintext input fallback")
        with patch.object(worker.getpass, "getpass", side_effect=unprotected):
            with self.assertRaisesRegex(worker.Blocker, "Protected terminal unavailable"):
                worker.private_prompt("Secret: ")

    def test_automatic_ansible_never_scans_binary_payload_as_inventory(self):
        profile = self.root / "profile"
        (profile / "vendor").mkdir(parents=True)
        (profile / "vendor/k3s").write_bytes(b"\x00\xffnot an inventory\x00")
        # Reproduce the old INI CSV trap: the empty path means the profile directory.
        (profile / "ansible.cfg").write_text("[defaults]\ninventory = localhost,\n")
        (profile / "playbook.yml").write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")
        real_run = subprocess.run
        calls = []
        def list_hosts(args, **kwargs):
            calls.append(args)
            result = real_run([str(Path(sys.executable).with_name("ansible-playbook")), *args[1:], "--list-hosts"],
                              cwd=kwargs["cwd"], env={**kwargs["env"], "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible")},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("localhost", result.stdout)
            self.assertNotIn("Unable to parse", result.stderr)
            return result
        with patch.object(worker, "PROFILE", profile), patch.object(worker.subprocess, "run", side_effect=list_hosts):
            worker.enroll()
        self.assertEqual(len(calls), 1)

    def test_routes_never_fall_back_to_lan(self):
        def fake(*args, **kwargs):
            if args[0] == "tailscale":
                return json.dumps({"BackendState": "Running", "TailscaleIPs": ["100.64.0.2"]})
            return json.dumps([{"dev": "eth0"}])
        with patch.object(worker, "load_config", return_value=self.config), patch.object(worker, "command", side_effect=fake):
            with self.assertRaisesRegex(worker.Blocker, "not tailscale0"):
                worker.tailscale()
        self.assertFalse((self.root / "tailscale-ip").exists())

    def test_joined_tailscale_preserves_identity_and_consumes_input_key(self):
        calls = []
        def fake(*args, **kwargs):
            calls.append(args)
            if args[0] == "tailscale":
                return json.dumps({"BackendState": "Running", "TailscaleIPs": ["100.64.0.2"]})
            return json.dumps([{"dev": "tailscale0"}])
        with patch.object(worker, "load_config", return_value=copy.deepcopy(self.config)), patch.object(worker, "command", side_effect=fake):
            worker.tailscale()
        self.assertFalse(any("up" in c for c in calls))
        self.assertNotIn("tailscale_auth_key", json.loads(worker.CONFIG.read_text()))
        self.assertEqual((self.root / "tailscale-ip").read_text(), "100.64.0.2")

    def test_readiness_requires_schedulable_resources_and_tailnet_address(self):
        good = self.node()
        self.assertIsNotNone(worker.node_resources(good, worker.identity(), "100.64.0.2"))
        bad = copy.deepcopy(good)
        bad["spec"]["unschedulable"] = True
        self.assertIsNone(worker.node_resources(bad, worker.identity(), "100.64.0.2"))
        bad = copy.deepcopy(good)
        bad["status"]["allocatable"].pop("ephemeral-storage")
        self.assertIsNone(worker.node_resources(bad, worker.identity(), "100.64.0.2"))
        bad = copy.deepcopy(good)
        bad["status"]["addresses"][0]["address"] = "192.168.1.10"
        with self.assertRaises(worker.Blocker):
            worker.node_resources(bad, worker.identity(), "100.64.0.2")

    def test_probe_uses_scheduler_and_resource_requests(self):
        pod = worker.probe_manifest(worker.identity(), "example@sha256:pin")
        self.assertNotIn("nodeName", pod["spec"])
        self.assertEqual(pod["spec"]["schedulerName"], "default-scheduler")
        self.assertEqual(set(pod["spec"]["containers"][0]["resources"]["requests"]), {"cpu", "memory", "ephemeral-storage"})
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])

    def test_verifier_retries_outage_then_requires_actual_running_pod(self):
        (self.root / "tailscale-ip").write_text("100.64.0.2")
        node = self.node()
        requests = []
        node_gets = 0
        pod_created = False
        def request(method, path, data=None):
            nonlocal node_gets, pod_created
            requests.append((method, path))
            if "/nodes/" in path:
                node_gets += 1
                return node if node_gets > 1 else None
            if method == "POST":
                pod_created = True
                return {}
            if method == "DELETE":
                return {"status": "Success"}
            return {"metadata": {"uid": "test-pod"}, "spec": {"nodeName": worker.identity(), "schedulerName": "default-scheduler"},
                    "status": {"phase": "Running", "conditions": [{"type": "PodScheduled", "status": "True"}]}} if pod_created else None
        class FakeApi:
            def __init__(self, config):
                pass
        FakeApi.request = staticmethod(request)
        with patch.object(worker, "load_config", return_value=copy.deepcopy(self.config)), patch.object(worker, "Api", FakeApi), patch.object(worker.time, "sleep"), patch.object(worker, "agent_active", return_value=True):
            worker.verify(timeout=5)
        final = json.loads((self.root / "status.json").read_text())
        self.assertTrue(final["success"])
        self.assertEqual(final["scheduler_proof"]["pod_uid"], "test-pod")
        self.assertEqual(len([r for r in requests if r[0] == "POST"]), 1)
        self.assertNotIn("verification_token", json.loads(worker.CONFIG.read_text()))

    def test_rejected_credentials_cannot_create_success(self):
        (self.root / "tailscale-ip").write_text("100.64.0.2")
        class Rejected:
            def __init__(self, config):
                pass
            def request(self, *args):
                raise worker.Blocker("Kubernetes rejected verification credentials")
        with patch.object(worker, "load_config", return_value=self.config), patch.object(worker, "Api", Rejected), patch.object(worker, "agent_active", return_value=True):
            with self.assertRaises(worker.Blocker):
                worker.verify(timeout=1)
        self.assertFalse(json.loads((self.root / "status.json").read_text())["success"])
        self.assertFalse((self.root / "proof.json").exists())

    def test_stale_ready_node_does_not_hide_an_inactive_local_agent(self):
        (self.root / "tailscale-ip").write_text("100.64.0.2")
        with patch.object(worker, "load_config", return_value=self.config), patch.object(worker, "Api") as api, patch.object(worker, "agent_active", return_value=False), patch.object(worker.time, "sleep"), patch.object(worker.time, "monotonic", side_effect=[0, 0, 1]):
            with self.assertRaisesRegex(worker.Blocker, "Verification timed out"):
                worker.verify(timeout=0.5)
        api.return_value.request.assert_not_called()
        self.assertFalse(json.loads((self.root / "status.json").read_text())["success"])

    def test_redirect_cannot_send_credentials_to_another_endpoint(self):
        with self.assertRaises(worker.Blocker):
            worker.NoRedirect().redirect_request(None, None, 302, "", {}, "https://192.168.0.1")


class InfrastructureContractTests(unittest.TestCase):
    def test_verifier_cannot_administer_cluster(self):
        resources = list(yaml.safe_load_all((ROOT / "provision/worker/verification-rbac.yaml").read_text()))
        cluster = next(x for x in resources if x["kind"] == "ClusterRole")
        self.assertEqual(cluster["rules"], [{"apiGroups": [""], "resources": ["nodes"], "resourceNames": ["NODE_NAME"], "verbs": ["get"]}])
        namespace = next(x for x in resources if x["kind"] == "Namespace")
        self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
        self.assertTrue(any(x["kind"] == "ResourceQuota" for x in resources))

    def test_install_never_preseeds_disk_or_destructive_acceptance(self):
        preseed = (ROOT / "images/laptop/preseed.cfg").read_text()
        for line in preseed.splitlines():
            if line.startswith("d-i "):
                self.assertNotRegex(line, r"partman-auto/disk\s")
                self.assertNotRegex(line, r"partman.*/confirm.*boolean true")
                self.assertNotRegex(line, r"passwd/.*password.*\s+\S+")
        self.assertIn("select-disk.sh", preseed)
        self.assertIn('test "$RET" = "$selected"', (ROOT / "images/laptop/select-disk.sh").read_text())

    def test_public_inputs_are_pinned_and_boots_are_installers(self):
        inputs = json.loads((ROOT / "images/laptop/inputs.json").read_text())
        for value in inputs.values():
            if isinstance(value, dict) and "url" in value:
                self.assertRegex(value["sha256"], r"^[a-f0-9]{64}$")
                self.assertNotIn("/latest/", value["url"])
        for filename in ("grub.cfg", "isolinux.cfg"):
            menu = (ROOT / "images/laptop" / filename).read_text()
            self.assertIn("/install.amd/vmlinuz", menu)
            self.assertIn("preseed/file=/cdrom/preseed.cfg", menu)
        build_source = (ROOT / "images/laptop/build.py").read_text()
        self.assertIn('"any", "replay"', build_source)


if __name__ == "__main__":
    unittest.main()

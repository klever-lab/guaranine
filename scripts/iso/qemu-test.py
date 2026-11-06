#!/usr/bin/env python3
"""Install the exact public ISO in disposable BIOS/UEFI guests; no production keys.

Real tailscaled clients enroll against a disposable Headscale + embedded DERP;
real K3s uses SQLite. This does not certify the hosted Tailscale policy or WAN.
Only a redacted result and package lists may leave the temporary test directory.
"""
import argparse
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time

import pexpect
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("iso_build", ROOT / "images/laptop/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
spec = importlib.util.spec_from_file_location("iso_finalize", Path(__file__).with_name("finalize.py"))
finalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finalizer)


def run(*args, data=None, timeout=300):
    return subprocess.check_output(args, input=data, stderr=subprocess.DEVNULL, timeout=timeout).decode().strip()


def eventually(check, timeout=180, interval=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (subprocess.SubprocessError, ValueError, KeyError):
            pass
        time.sleep(interval)
    raise AssertionError("Timed out waiting for disposable test infrastructure")


class Hub:
    def __init__(self, directory, iso):
        self.directory = directory
        self.name = "guaranine-test-" + secrets.token_hex(4)
        self.network = self.name
        self.ip = f"10.177.{secrets.randbelow(200) + 20}.2"
        self.prefix = self.ip.rsplit(".", 1)[0] + ".0/24"
        run("xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", "/guaranine/worker/vendor", str(directory / "vendor"))
        for name in ("k3s", "tailscale", "tailscaled"):
            shutil.copyfile(directory / "vendor" / name, directory / name)
            (directory / name).chmod(0o755)
        build.fetch(build.INPUTS["headscale_test"], directory / "headscale")
        (directory / "headscale").chmod(0o755)
        shutil.copyfile(ROOT / "scripts/iso/test-coordinator.sh", directory / "test-coordinator.sh")
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=Disposable ISO Test CA",
            "-keyout", str(directory / "ca.key"), "-out", str(directory / "ca.crt"))
        run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=" + self.ip,
            "-keyout", str(directory / "server.key"), "-out", str(directory / "server.csr"))
        (directory / "tls.ext").write_text(f"subjectAltName=IP:{self.ip}\nextendedKeyUsage=serverAuth\n")
        run("openssl", "x509", "-req", "-days", "2", "-in", str(directory / "server.csr"), "-CA", str(directory / "ca.crt"),
            "-CAkey", str(directory / "ca.key"), "-CAcreateserial", "-extfile", str(directory / "tls.ext"), "-out", str(directory / "server.crt"))
        config = {"server_url": f"https://{self.ip}:8443", "listen_addr": "0.0.0.0:8443", "metrics_listen_addr": "127.0.0.1:9090",
                  "noise": {"private_key_path": "/var/lib/headscale/noise.key"},
                  "prefixes": {"v4": "100.64.0.0/24", "v6": "fd7a:115c:a1e0::/48", "allocation": "sequential"},
                  "database": {"type": "sqlite", "sqlite": {"path": "/var/lib/headscale/db.sqlite"}},
                  "tls_cert_path": "/fixture/server.crt", "tls_key_path": "/fixture/server.key",
                  "dns": {"magic_dns": False, "base_domain": "iso.test", "override_local_dns": False},
                  "derp": {"server": {"enabled": True, "region_id": 999, "region_code": "iso", "region_name": "Disposable ISO DERP",
                                      "verify_clients": True, "stun_listen_addr": "0.0.0.0:3478", "private_key_path": "/var/lib/headscale/derp.key",
                                      "automatically_add_embedded_derp_region": True, "ipv4": self.ip},
                           "urls": [], "paths": [], "auto_update_enabled": False},
                  "log": {"level": "warn"}, "disable_check_updates": True}
        (directory / "config.yaml").write_text(yaml.safe_dump(config))

    def exec(self, *args, data=None):
        return run("docker", "exec", "-i", self.name, *args, data=data)

    def kubectl(self, *args, data=None):
        return self.exec("/fixture/k3s", "kubectl", "--server=https://100.64.0.1:6443", *args, data=data)

    def start(self):
        run("docker", "network", "create", "--subnet", self.prefix, self.network)
        run("docker", "run", "--detach", "--name", self.name, "--privileged", "--network", self.network, "--ip", self.ip,
            "--mount", f"type=bind,source={self.directory},target=/fixture", "--env", "TEST_CONTROL_IP=" + self.ip,
            "guaranine-iso-test")
        eventually(lambda: self.kubectl("get", "--raw=/readyz") == "ok", timeout=240)

    def credentials(self, node):
        namespace = "enroll-" + node[7:]
        rbac = (ROOT / "provision/worker/verification-rbac.yaml").read_text().replace("VERIFY_NAMESPACE", namespace).replace("NODE_NAME", node)
        self.kubectl("apply", "-f", "-", data=rbac.encode())
        ca = self.exec("cat", "/var/lib/rancher/k3s/server/tls/server-ca.crt") + "\n"
        token = self.exec("/fixture/k3s", "token", "create", "--ttl", "1h")
        if not token.startswith("K10"):
            token = "K10" + hashlib.sha256(ca.encode()).hexdigest() + "::" + token
        auth = json.loads(self.exec("/fixture/headscale", "preauthkeys", "create", "--user", "1", "--expiration", "2h", "--output", "json"))["key"]
        verifier = self.kubectl("-n", namespace, "create", "token", "enrollment-verifier", "--duration=1h")
        address = self.exec("/fixture/tailscale", "ip", "-4")
        return {"server": f"https://{address}:6443", "server_ca": ca, "bootstrap_token": token, "tailscale_auth_key": auth,
                "verification_namespace": namespace, "verification_token": verifier,
                "control_url": f"https://{self.ip}:8443", "control_ca": (self.directory / "ca.crt").read_text()}

    def pause(self):
        run("docker", "pause", self.name)

    def resume(self):
        run("docker", "unpause", self.name)

    def close(self):
        with contextlib.suppress(subprocess.SubprocessError):
            run("docker", "unpause", self.name)
        with contextlib.suppress(subprocess.SubprocessError):
            run("docker", "rm", "--force", self.name)
        with contextlib.suppress(subprocess.SubprocessError):
            run("docker", "network", "rm", self.network)


class Guest:
    def __init__(self, directory, iso, firmware):
        self.directory, self.iso, self.firmware = directory, iso, firmware
        self.disk = directory / "target.qcow2"
        self.password = secrets.token_urlsafe(24)
        self.process = None
        self.root_available = False
        run("qemu-img", "create", "-f", "qcow2", str(self.disk), "20G")
        self.firmware_args = []
        if firmware == "uefi":
            code = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
            variables = directory / "OVMF_VARS.fd"
            shutil.copyfile("/usr/share/OVMF/OVMF_VARS_4M.fd", variables)
            self.firmware_args = ["-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                                  "-drive", f"if=pflash,format=raw,file={variables}"]

    def boot(self, installer=False):
        self.root_available = False
        args = ["-enable-kvm", "-cpu", "host", "-m", "2048", "-smp", "2", "-nographic", "-monitor", "none",
                "-drive", f"file={self.disk},if=virtio,format=qcow2", "-no-reboot",
                "-netdev", "user,id=internet,ipv6=off", "-device", "virtio-net-pci,netdev=internet", *self.firmware_args]
        if installer:
            args += ["-cdrom", str(self.iso), "-boot", "d"]
        else:
            args += ["-boot", "c"]
        self.process = pexpect.spawn("qemu-system-x86_64", args, encoding="utf-8", codec_errors="replace", timeout=300)
        # Never log raw terminal streams: interactive secrets must stay private.
        self.process.delaybeforesend = 0.1

    def install(self):
        self.boot(installer=True)
        p = self.process
        if self.firmware == "bios":
            p.expect("boot:")
            p.sendline("serial")
        else:
            p.expect("Install worker \\(serial console\\)")
            time.sleep(1)
            p.send("\x1b[B\r")
        disk_selected = False
        disk_confirmed = False
        target_confirmed = False
        wrong_target_rejected = False
        patterns = [r"(?:Choose a password|Password) for the new user:", r"Re-enter password to verify:",
                    r"Installation disk:", r"Write the changes to disks\?", r"Finish partitioning and write changes to disk",
                    r"Partitioning method:", r"Force UEFI installation\?", r"Install the GRUB boot loader.*\?",
                    r"Device for boot loader installation:", pexpect.EOF, r"!! ERROR:", r"Confirm target disk:"]
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            matched = p.expect(patterns, timeout=600)
            print(f"QEMU {self.firmware}: installer checkpoint {matched}", flush=True)
            if matched in (0, 1):
                p.sendline(self.password)
            elif matched == 2:
                disk_selected = True
                p.expect(r"Prompt:")
                p.sendline("1")
            elif matched == 3:
                disk_confirmed = True
                p.expect(r"Prompt:")
                p.sendline("1")
            elif matched == 4:
                # Text frontend's menu includes this option; select its number.
                menu = p.before + p.after
                match = re.search(r"(\d+):\s+Finish partitioning", menu)
                p.expect(r"Prompt:")
                p.sendline(match[1] if match else "1")
            elif matched in (6, 7):
                p.expect(r"Prompt:")
                p.sendline("1")
            elif matched == 8:
                p.sendline("/dev/vda")
            elif matched == 5:
                p.sendline("1")
            elif matched == 9:
                p.close()
                assert p.exitstatus == 0, "Installer guest did not reboot cleanly"
                assert disk_selected and disk_confirmed and target_confirmed and wrong_target_rejected, "Installer skipped explicit disk selection or confirmation"
                return
            elif matched == 11:
                if not wrong_target_rejected:
                    p.sendline("/dev/not-the-selected-disk")
                    wrong_target_rejected = True
                else:
                    target_confirmed = True
                    p.sendline("/dev/vda")
            else:
                p.expect(r"([^\r\n]+)\r*\n")
                error = p.match[1].strip()
                allowed = {"Bad archive mirror", "Failed to run preseeded command", "Software installation step failed", "Installation step failed", "Reserved username"}
                if error in allowed:
                    raise AssertionError("Debian installer reported: " + error)
                raise AssertionError("Debian installer reported an error; no release is permitted")
        raise AssertionError("Installer did not finish before the time bound")

    def setup(self, credentials=None):
        p = self.process
        p.expect(r"Node identity: (worker-[a-f0-9]{20})")
        self.node = p.match[1]
        return self.node

    def input_credentials(self, config):
        self.process.expect(r"Runtime JSON file under /run[^\r\n]*: ")
        self.process.sendline("")
        labels = [("Coordinator tailnet endpoint", "server"), ("Server CA PEM", "server_ca"),
                  ("Secure expiring K3s bootstrap token", "bootstrap_token"), ("Scoped single-use Tailscale auth key", "tailscale_auth_key"),
                  ("Per-worker verification namespace", "verification_namespace"), ("Short-lived verification service account token", "verification_token"),
                  ("Tailscale control URL", "control_url"), ("Private control CA PEM", "control_ca")]
        for label, key in labels:
            self.process.expect(re.escape(label) + r"[^\r\n]*: ")
            value = config.get(key, "")
            if key in ("server_ca", "control_ca"):
                value = base64.b64encode(value.encode()).decode()
            self.process.sendline(value)

    def login(self):
        self.process.expect("login:")
        self.process.sendline("fleetadmin")
        self.process.expect("Password:")
        self.process.sendline(self.password)
        self.process.expect(r"\$ ")
        self.process.sendline("sudo -S -p 'SUDO_PASSWORD: ' /bin/bash --noprofile --norc")
        self.process.expect("SUDO_PASSWORD: ")
        self.process.sendline(self.password)
        self.process.expect(r"# ")
        self.process.sendline("export PS1='GUARANINE_TEST_PROMPT> '")
        self.process.expect_exact("GUARANINE_TEST_PROMPT> ")
        self.process.expect_exact("GUARANINE_TEST_PROMPT> ")
        self.process.sendline("stty -echo")
        self.process.expect_exact("GUARANINE_TEST_PROMPT> ")
        self.root_available = True

    def shell(self, script, timeout=120):
        self.process.sendline(script)
        self.process.expect_exact("GUARANINE_TEST_PROMPT> ", timeout=timeout)
        return self.process.before.strip()

    def state(self):
        output = self.shell("cat /var/lib/guaranine/status.json")
        start, end = output.find("{"), output.rfind("}")
        return json.loads(output[start:end + 1])

    def replace(self, config):
        self.process.sendline("guaranine-worker configure")
        self.setup()
        self.input_credentials(config)
        self.process.expect_exact("GUARANINE_TEST_PROMPT> ")

    def close(self):
        if self.process:
            self.process.close(force=True)


def test(iso, firmware, output):
    results = {"schema": 1, "iso_sha256": build.digest(iso), "source_commit": json.loads((iso.parent / "build.json").read_text())["source_commit"],
               "test_control": "disposable Headscale; actual Tailscale clients and K3s", "hosted_tailscale_tested": False,
               "qemu_version": run("qemu-system-x86_64", "--version").splitlines()[0], "firmware": {}}
    with tempfile.TemporaryDirectory(prefix="guaranine-qemu-") as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        fixture = directory / "hub"
        fixture.mkdir(mode=0o700)
        hub = Hub(fixture, iso)
        identities = []
        try:
            hub.start()
            for mode in firmware:
                print(f"QEMU {mode}: installing the public ISO", flush=True)
                guest_dir = directory / mode
                guest_dir.mkdir()
                guest = Guest(guest_dir, iso, mode)
                try:
                    guest.install()
                    print(f"QEMU {mode}: installer rebooted; testing automatic enrollment", flush=True)
                    guest.boot()
                    node = guest.setup()
                    print(f"QEMU {mode}: protected first-boot setup reached", flush=True)
                    config = hub.credentials(node)
                    rejected = dict(config)
                    if mode == "bios":
                        rejected["verification_token"] = "invalid-disposable-verification-token"
                    else:
                        rejected["tailscale_auth_key"] = "invalid-disposable-auth-key"
                    guest.input_credentials(rejected)
                    print(f"QEMU {mode}: waiting for local recovery console", flush=True)
                    guest.login()
                    eventually(lambda: guest.state().get("stage") == "blocked", timeout=240)
                    assert not guest.state()["success"], "Rejected credentials produced false enrollment success"
                    print(f"QEMU {mode}: rejected credentials did not report success", flush=True)
                    replacement = hub.credentials(node)
                    bootstrap_ids = {c["bootstrap_token"].rsplit("::", 1)[1].split(".", 1)[0] for c in (config, replacement)}
                    hub.pause()
                    try:
                        guest.replace(config)
                        time.sleep(15)
                        # Replace credentials while an attempt is waiting on the
                        # outage, proving stale attempts cannot overwrite input.
                        guest.replace(replacement)
                        config = replacement
                        time.sleep(20)
                        assert not guest.state()["success"], "Coordinator outage produced false enrollment success"
                    finally:
                        hub.resume()
                    state = eventually(lambda: (s if (s := guest.state()).get("success") else None), timeout=600)
                    print(f"QEMU {mode}: recovered automatically after coordinator outage", flush=True)
                    assert state["scheduler_proof"]["scheduler"] == "default-scheduler"
                    assert set(state["allocatable"]) == {"cpu", "memory", "ephemeral-storage"}
                    # Credentials used only during setup must have been consumed.
                    clean = guest.shell("python3 -c \"import json; c=json.load(open('/var/lib/guaranine/enrollment.json')); print(all(k not in c for k in ('tailscale_auth_key','bootstrap_token','verification_token')))\"")
                    assert "True" in clean
                    machine = guest.shell("cat /etc/machine-id")
                    ssh = guest.shell("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub")
                    identities.append((node, machine, ssh))
                    packages = finalizer.package_records(guest.shell("dpkg-query -W '-f=${Package}=${Version}\\n'", timeout=60))
                    before_nodes = json.loads(hub.kubectl("get", "nodes", "-o", "json"))
                    expected_workers = {entry[0] for entry in identities}
                    assert {n["metadata"]["name"] for n in before_nodes["items"] if n["metadata"]["name"].startswith("worker-")} == expected_workers
                    # Revocation proves reboot uses the node's own client identity,
                    # rather than accidentally relying on unexpired bootstrap/RBAC.
                    for bootstrap_id in bootstrap_ids:
                        hub.exec("/fixture/k3s", "token", "delete", bootstrap_id)
                    hub.kubectl("delete", "namespace", config["verification_namespace"], "--wait=false")
                    hub.kubectl("delete", "clusterrole,clusterrolebinding", config["verification_namespace"])
                    guest.process.sendline("systemctl reboot")
                    guest.process.expect(pexpect.EOF, timeout=120)
                    guest.close()
                    guest.boot()
                    guest.login()
                    rebooted = eventually(lambda: (s if (s := guest.state()).get("success") else None), timeout=240)
                    assert rebooted["node"] == node and machine == guest.shell("cat /etc/machine-id")
                    assert ssh == guest.shell("ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub")
                    after_nodes = json.loads(hub.kubectl("get", "nodes", "-o", "json"))
                    assert {n["metadata"]["uid"] for n in before_nodes["items"]} == {n["metadata"]["uid"] for n in after_nodes["items"]}
                    # Block physical WireGuard UDP to force the embedded relay.
                    hub.exec("iptables", "-I", "OUTPUT", "!", "-o", "tailscale0", "-p", "udp", "!", "--dport", "53", "-j", "DROP")
                    ping = hub.exec("/fixture/tailscale", "ping", "--c=3", "--until-direct=false", state["tailscale_ip"])
                    assert "DERP" in ping, "No proven relay path"
                    results["firmware"][mode] = {"installation": "passed", "explicit_disk_confirmation": "passed", "automatic_enrollment": "passed",
                        "rejected_credentials": "passed", "coordinator_outage_retry": "passed", "reboot_identity_and_no_duplicates": "passed",
                        "credential_replacement_while_retrying": "passed",
                        "reboot_after_bootstrap_and_verifier_revocation": "passed",
                        "resource_requests_scheduled": "passed", "embedded_derp": "passed", "installed_packages": packages}
                    print(f"QEMU {mode}: required enrollment and reboot checks passed", flush=True)
                except (AssertionError, pexpect.ExceptionPexpect, subprocess.SubprocessError):
                    if guest.root_available:
                        print(guest.shell("systemctl show guaranine-enroll.service tailscaled.service NetworkManager-wait-online.service -p Id -p ActiveState -p SubState -p Result -p Job"), flush=True)
                        print(guest.shell("systemctl list-jobs --no-pager"), flush=True)
                        print(guest.shell("free -m"), flush=True)
                        print(guest.shell("journalctl --dmesg --grep='oom|Out of memory|Killed process' --no-pager -n 12"), flush=True)
                        print(json.dumps(guest.state()), flush=True)
                    raise
                finally:
                    guest.close()
            if len(identities) > 1:
                for index in range(3):
                    assert len({identity[index] for identity in identities}) == len(identities), "Cloned host identity detected"
                results["unique_identities"] = "passed"
            else:
                results["unique_identities"] = "requires both firmware guests"
        finally:
            hub.close()
    output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso", type=Path)
    parser.add_argument("--firmware", choices=["bios", "uefi", "all"], default="all")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        test(args.iso.resolve(), ["bios", "uefi"] if args.firmware == "all" else [args.firmware], args.output)
    except (pexpect.ExceptionPexpect, subprocess.SubprocessError) as error:
        # Do not print exceptions containing terminal buffers, command input or keys.
        raise SystemExit("QEMU test failed: " + type(error).__name__ + "; no release is permitted") from None

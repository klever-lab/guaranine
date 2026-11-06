#!/usr/bin/env python3
"""Protected local enrollment driver; the Kubernetes API remains the cluster API."""
import argparse
import base64
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings

STATE = Path("/var/lib/guaranine")
PROFILE = Path("/usr/lib/guaranine")
CONFIG = STATE / "enrollment.json"
RUNTIME = Path("/run/guaranine")
TAILNET = ipaddress.ip_network("100.64.0.0/10")


class Blocker(Exception):
    """Only fixed, secret-free messages may be exposed to the console/journal."""


def command(*args, timeout=60, input_data=None):
    try:
        result = subprocess.run(args, input=input_data, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=True)
        return result.stdout.decode().strip()
    except (subprocess.SubprocessError, OSError):
        raise Blocker("Command failed; check network, credential validity and the current stage") from None


def write_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".new")
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    temporary.replace(path)


def status(stage, detail, **values):
    write_json(STATE / "status.json", {"stage": stage, "detail": detail,
               "success": stage == "enrolled", "checked_at": int(time.time()), **values})
    print(f"Guaranine: {stage}: {detail}", flush=True)


def identity():
    STATE.mkdir(mode=0o700, exist_ok=True)
    path = STATE / "node-name"
    if not path.exists():
        # O_EXCL prevents concurrent setup/retry processes inventing another identity.
        name = "worker-" + uuid.uuid4().hex[:20]
        with path.open("x") as stream:
            stream.write(name + "\n")
        path.chmod(0o600)
    name = path.read_text().strip()
    if not re.fullmatch(r"worker-[0-9a-f]{20}", name):
        raise Blocker("Invalid saved node identity; preserve it and investigate before re-enrollment")
    return name


def load_config():
    try:
        metadata = CONFIG.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise Blocker("Enrollment input must be a root-owned regular file with mode 0600")
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        raise Blocker("Enrollment input missing or invalid; run sudo guaranine-worker configure") from None


def validate_config(config):
    try:
        url = urllib.parse.urlsplit(config["server"])
        address = ipaddress.ip_address(url.hostname)
        assert url.scheme == "https" and url.port == 6443 and address in TAILNET
        assert not url.username and not url.password and url.path in ("", "/") and not url.query and not url.fragment
        token = config.get("bootstrap_token", "")
        # Bootstrap credentials only: never accept the all-powerful server token or
        # the default agent-token symlink. k3s token create emits this secure format.
        if token:
            match = re.fullmatch(r"K10([a-f0-9]{64})::([a-z0-9]{6}\.[a-z0-9]{16})", token)
            assert match and hashlib.sha256(config["server_ca"].encode()).hexdigest() == match[1]
        ssl.create_default_context(cadata=config["server_ca"])
        if config.get("verification_token"):
            assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", config["verification_namespace"])
        control = urllib.parse.urlsplit(config.get("control_url", "https://controlplane.tailscale.com"))
        assert control.scheme == "https" and control.hostname and not control.username and not control.password
        assert not control.query and not control.fragment
        if config.get("control_ca"):
            ssl.create_default_context(cadata=config["control_ca"])
    except (KeyError, ValueError, AssertionError, ssl.SSLError, TypeError):
        raise Blocker("Invalid configuration: require a tailnet IPv4 HTTPS endpoint on 6443, CA PEM and matching secure bootstrap token") from None
    return config


def private_prompt(label):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(label)
    except getpass.GetPassWarning:
        raise Blocker("Protected terminal unavailable; use a local console or a root-only runtime input file") from None


def configure(input_file=None):
    name = identity()
    print(f"\nNode identity: {name}\nKeep this identity for credential replacement.", flush=True)
    print("Connect internet using Ethernet or, on another console, sudo nmtui.")
    print("Inputs are hidden and saved root-only. Ctrl-C leaves enrollment incomplete.")
    if input_file is None:
        input_file = private_prompt("Runtime JSON file under /run (blank for private prompts): ").strip()
    config = {}
    def ask(key, label, default=""):
        config[key] = private_prompt(label + ": ").strip() or default
    temporary_input = None
    if input_file:
        temporary_input = Path(input_file)
        metadata = temporary_input.lstat()
        if not temporary_input.resolve().is_relative_to("/run") or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise Blocker("Runtime input must be a root-owned mode-0600 regular file under /run")
        config = json.loads(temporary_input.read_text())
    else:
        ask("server", "Coordinator tailnet endpoint (https://100.x.y.z:6443)")
        ask("server_ca", "Server CA PEM (base64, one line)")
        ask("bootstrap_token", "Secure expiring K3s bootstrap token")
        ask("tailscale_auth_key", "Scoped single-use Tailscale auth key (blank preserves joined identity)")
        ask("verification_namespace", "Per-worker verification namespace")
        ask("verification_token", "Short-lived verification service account token")
        ask("control_url", "Tailscale control URL (blank uses hosted Tailscale)", "https://controlplane.tailscale.com")
        ask("control_ca", "Private control CA PEM base64 (blank for hosted Tailscale)")
        try:
            for key in ("server_ca", "control_ca"):
                if config[key]:
                    config[key] = base64.b64decode(config[key], validate=True).decode()
        except (ValueError, UnicodeError):
            raise Blocker("CA input must be base64-encoded PEM") from None
    validate_config(config)
    # Do not silently retarget an already enrolled machine to a different cluster.
    if CONFIG.exists():
        previous = load_config()
        if (previous["server"], previous["server_ca"]) != (config["server"], config["server_ca"]):
            raise Blocker("Cluster identity changed; drain and explicitly re-enroll before replacing cluster trust")
    # A previous attempt may still be waiting on the coordinator. Quiesce only
    # the provisioning controller, leaving tailscaled and the agent identity
    # intact, so it cannot overwrite replacement input with an older copy.
    command("systemctl", "stop", "guaranine-enroll.timer", "guaranine-enroll.service", timeout=120)
    try:
        write_json(CONFIG, config)
        if temporary_input:
            temporary_input.unlink(missing_ok=True)
        (STATE / "configured").touch(mode=0o600)
        (STATE / "proof.json").unlink(missing_ok=True)
        status("configured", "Input saved; automatic enrollment will retry until verified")
    finally:
        command("systemctl", "start", "guaranine-enroll.timer")
    command("systemctl", "start", "--no-block", "guaranine-enroll.service")


def preflight():
    status("preflight", "Checking host, cluster trust and persistent identity")
    if Path("/etc/systemd/system/k3s.service").exists() or Path("/var/lib/rancher/k3s/server").exists():
        raise Blocker("A laptop must never run the K3s server role")
    validate_config(load_config())
    name = identity()
    Path("/etc/hostname").write_text(name + "\n")
    command("hostnamectl", "set-hostname", name)
    for directory in ("/etc/systemd/logind.conf.d", "/etc/systemd/system/k3s-agent.service.d"):
        Path(directory).mkdir(parents=True, exist_ok=True)


def tailscale():
    status("tailscale", "Waiting for authenticated Tailscale transport; no LAN fallback")
    config = load_config()
    if config.get("control_ca"):
        Path("/usr/local/share/ca-certificates/guaranine-control.crt").write_text(config["control_ca"])
        command("update-ca-certificates")
        # tailscaled reads the trust store at start; restart only when installing CA.
        marker = STATE / "control-ca.sha256"
        ca_hash = hashlib.sha256(config["control_ca"].encode()).hexdigest()
        if not marker.exists() or marker.read_text() != ca_hash:
            command("systemctl", "restart", "tailscaled")
            marker.write_text(ca_hash)
    info = json.loads(command("tailscale", "status", "--json"))
    if info.get("BackendState") != "Running":
        key = config.get("tailscale_auth_key")
        if not key:
            raise Blocker("Tailscale needs a replacement enrollment key; run sudo guaranine-worker configure")
        keypath = RUNTIME / "tailscale.key"
        keypath.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        keypath.write_text(key)
        keypath.chmod(0o600)
        try:
            command("tailscale", "up", "--auth-key=file:" + str(keypath),
                    "--hostname=" + identity(), "--accept-routes=false", "--accept-dns=false",
                    "--ssh=false", "--login-server=" + config.get("control_url", "https://controlplane.tailscale.com"),
                    "--timeout=45s", timeout=55)
        except Blocker:
            raise Blocker("Tailscale join failed: check internet, control trust, key expiry and device approval") from None
        finally:
            keypath.unlink(missing_ok=True)
    info = json.loads(command("tailscale", "status", "--json"))
    if info.get("BackendState") != "Running":
        raise Blocker("Tailscale is not authenticated; replace expired or rejected credentials")
    addresses = [ip for ip in info.get("TailscaleIPs", []) if ipaddress.ip_address(ip).version == 4 and ipaddress.ip_address(ip) in TAILNET]
    if len(addresses) != 1:
        raise Blocker("No unique Tailscale IPv4 address; waiting for tailnet connectivity")
    server_ip = urllib.parse.urlsplit(config["server"]).hostname
    route = json.loads(command("ip", "-j", "route", "get", server_ip))
    if not route or route[0].get("dev") != "tailscale0":
        raise Blocker("Coordinator route is not tailscale0; refusing cluster transport")
    (STATE / "tailscale-ip").write_text(addresses[0])
    # Keep tailscaled's own state; consume the one-time enrollment key.
    config.pop("tailscale_auth_key", None)
    write_json(CONFIG, config)


def agent_config():
    status("agent", "Configuring pinned K3s agent on tailscale0")
    config = load_config()
    address = (STATE / "tailscale-ip").read_text().strip()
    path = Path("/etc/rancher/k3s/config.yaml")
    tokenpath = Path("/etc/rancher/k3s/bootstrap-token")
    token_changed = False
    if config.get("bootstrap_token"):
        token = config.pop("bootstrap_token")
        token_changed = not tokenpath.exists() or tokenpath.read_text() != token
        tokenpath.write_text(token)
        tokenpath.chmod(0o600)
        write_json(CONFIG, config)
    if not tokenpath.exists():
        raise Blocker("Missing K3s bootstrap credential; supply replacement credentials")
    # JSON is valid YAML. No secrets in arguments, environment or templating logs.
    value = {"server": config["server"], "token-file": str(tokenpath), "node-name": identity(),
             "node-ip": address, "bind-address": address, "flannel-iface": "tailscale0",
             "kubelet-arg": ["address=" + address, "system-reserved=cpu=100m,memory=256Mi,ephemeral-storage=1Gi",
                             "kube-reserved=cpu=100m,memory=256Mi,ephemeral-storage=1Gi"]}
    previous = path.read_text() if path.exists() else ""
    write_json(path, value)
    if (previous != path.read_text() or token_changed) and Path("/etc/systemd/system/k3s-agent.service").exists():
        command("systemctl", "try-restart", "k3s-agent", timeout=120)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        raise Blocker("Coordinator redirected the API request; refusing alternate cluster transport")


class Api:
    def __init__(self, config):
        self.server = config["server"].rstrip("/")
        self.context = ssl.create_default_context(cadata=config["server_ca"])
        self.token = config.get("verification_token")
        if not self.token:
            self.context.load_cert_chain("/var/lib/rancher/k3s/agent/client-kubelet.crt",
                                         "/var/lib/rancher/k3s/agent/client-kubelet.key")
        # Never inherit proxy settings for private cluster transport.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=self.context))

    def request(self, method, path, data=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.server + path, headers=headers, method=method,
                                         data=json.dumps(data).encode() if data is not None else None)
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise Blocker("Kubernetes rejected verification credentials; replace the expired or incorrectly scoped token") from None
            if error.code in (404, 409):
                return None
            raise Blocker("Kubernetes API request failed; coordinator enrollment remains incomplete") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise Blocker("Coordinator TLS trust validation failed; check endpoint and the trusted server CA") from None
            return None
        except (OSError, ValueError):
            return None


def node_resources(node, name, address):
    if not node or node.get("metadata", {}).get("name") != name:
        return None
    spec = node.get("spec", {})
    details = node.get("status", {})
    if spec.get("unschedulable") or any(t.get("effect") in ("NoSchedule", "NoExecute") for t in spec.get("taints", [])):
        return None
    if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in details.get("conditions", [])):
        return None
    internal = [a["address"] for a in details.get("addresses", []) if a["type"] == "InternalIP"]
    if internal != [address] or any(a["type"] == "ExternalIP" for a in details.get("addresses", [])):
        raise Blocker("Node advertises a non-tailnet address; refusing enrollment success")
    allocatable = details.get("allocatable", {})
    if not all(allocatable.get(key) and re.search(r"[1-9]", allocatable[key]) for key in ("cpu", "memory", "ephemeral-storage")):
        return None
    return {key: allocatable[key] for key in ("cpu", "memory", "ephemeral-storage")}


def probe_manifest(name, image):
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "enrollment-probe"},
            "spec": {"schedulerName": "default-scheduler", "restartPolicy": "Never", "automountServiceAccountToken": False,
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
                     "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
                         {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": [name]}]}]}}},
                     "containers": [{"name": "probe", "image": image,
                                     "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                                     "resources": {"requests": {"cpu": "10m", "memory": "16Mi", "ephemeral-storage": "16Mi"},
                                                   "limits": {"cpu": "50m", "memory": "32Mi", "ephemeral-storage": "32Mi"}}}]}}


def agent_active():
    result = subprocess.run(["systemctl", "is-active", "k3s-agent"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False, timeout=15)
    return result.returncode == 0 and result.stdout.strip() == b"active"


def verify(timeout=480):
    status("verification", "Waiting for Ready, schedulable capacity and scheduler proof")
    config = load_config()
    name = identity()
    address = (STATE / "tailscale-ip").read_text().strip()
    proof_path = STATE / "proof.json"
    proof = json.loads(proof_path.read_text()) if proof_path.exists() else None
    if not proof and not config.get("verification_token"):
        raise Blocker("Scheduler proof missing; supply a short-lived per-worker verification token")
    api = Api(config)
    podpath = "/api/v1/namespaces/" + config.get("verification_namespace", "") + "/pods"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # In particular after reboot, an old Ready condition alone must not be
        # mistaken for a functioning local agent. Upstream Type=notify becomes
        # active after its startup has completed.
        if not agent_active():
            time.sleep(5)
            continue
        node = api.request("GET", "/api/v1/nodes/" + name)
        resources = node_resources(node, name, address)
        if resources:
            if not proof:
                if not config.get("verification_token"):
                    raise Blocker("Scheduler proof missing; supply a short-lived per-worker verification token")
                pod = api.request("GET", podpath + "/enrollment-probe")
                if not pod:
                    image = json.loads((PROFILE / "inputs.json").read_text())["probe_image"]
                    api.request("POST", podpath, probe_manifest(name, image))
                elif pod.get("spec", {}).get("nodeName") == name and pod.get("status", {}).get("phase") == "Running" and any(
                        c.get("type") == "PodScheduled" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])):
                    if pod["spec"].get("schedulerName") != "default-scheduler":
                        raise Blocker("Probe was not placed by the standard scheduler")
                    proof = {"node": name, "node_uid": node["metadata"]["uid"], "pod_uid": pod["metadata"]["uid"],
                             "scheduler": "default-scheduler", "verified_at": int(time.time())}
                    # Cleanup must succeed before discarding its credential.
                    if api.request("DELETE", podpath + "/enrollment-probe", {"gracePeriodSeconds": 0}) is None:
                        time.sleep(3)
                        continue
                    write_json(proof_path, proof)
                    config.pop("verification_token", None)
                    write_json(CONFIG, config)
            if proof and proof["node"] == name and proof["node_uid"] == node["metadata"]["uid"]:
                status("enrolled", "Node Ready, schedulable and scheduler probe verified", node=name,
                       tailscale_ip=address, allocatable=resources, scheduler_proof=proof)
                return
        time.sleep(5)
    raise Blocker("Verification timed out: check bootstrap expiry, coordinator version/CA, ACLs, node taints, capacity and image egress; no success recorded")


def enroll():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "enroll.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        environment = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                       "ANSIBLE_CONFIG": str(PROFILE / "ansible.cfg"), "ANSIBLE_NO_LOG": "true"}
        # All Ansible output is suppressed, including failed module arguments. Each
        # helper stage reports a fixed, redacted status instead of secret payloads.
        result = subprocess.run(["ansible-playbook", "--inventory", "localhost,", str(PROFILE / "playbook.yml")], cwd=PROFILE, env=environment,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=690)
        if result.returncode:
            current = json.loads((STATE / "status.json").read_text()) if (STATE / "status.json").exists() else {}
            if current.get("stage") != "blocked":
                status("blocked", "Provisioning failed during " + current.get("stage", "Ansible startup") + "; retry scheduled, replace credentials if expired")
            raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["configure", "enroll", "stage", "status"])
    parser.add_argument("stage", nargs="?", choices=["preflight", "tailscale", "agent-config", "verify"])
    parser.add_argument("--input-file", help="Protected runtime JSON under /run; consumed after validation")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run as root on this worker's console")
    os.umask(0o077)
    try:
        if args.action == "status":
            print((STATE / "status.json").read_text())
        elif args.action == "stage":
            {"preflight": preflight, "tailscale": tailscale, "agent-config": agent_config, "verify": verify}[args.stage]()
        elif args.action == "configure":
            configure(args.input_file)
        else:
            enroll()
    except Blocker as error:
        status("blocked", str(error))
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        status("blocked", "Local input, host dependency or timeout failure; enrollment remains incomplete and will retry")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

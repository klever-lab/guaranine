#!/usr/bin/env python3
"""Validate both roles with real pinned talosctl and lint the runnable tooling."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
from guaranine import boot_config, fetch, generate, initialize, talosctl

ACTIONLINT = {
    'linux_amd64': '023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757',
    'linux_arm64': '401942f9c24ed71e4fe71b76c7d638f66d8633575c4016efd2977ce7c28317d0',
    'darwin_amd64': '28e5de5a05fc558474f638323d736d822fff183d2d492f0aecb2b73cc44584f5',
    'darwin_arm64': '2693315b9093aeacb4ebd91a993fea54fc215057bf0da2659056b4bc033873db',
}


def main():
    os.umask(0o077)
    json.loads((ROOT / 'tailscale/policy.json').read_text())
    schematic = yaml.safe_load((ROOT / 'talos/schematic.yaml').read_text())
    if schematic != {'customization': {'systemExtensions': {'officialExtensions': ['siderolabs/tailscale']}}}:
        raise ValueError('Public schematics may contain only the declared extension; never machine configuration or credentials')
    with tempfile.TemporaryDirectory(prefix='guaranine-talos-validation-') as scratch:
        scratch = Path(scratch)
        initialize(scratch / 'cluster')
        boot = scratch / 'boot.yaml'
        boot.write_text(boot_config('tskey-auth-DISPOSABLE-MAINTENANCE'))
        boot.chmod(0o600)
        subprocess.run([talosctl(), 'validate', '--strict', '--mode', 'metal', '--config', str(boot)], check=True)
        for role in ('controlplane', 'worker'):
            now = datetime.now(timezone.utc)
            key = scratch / (role + '-key.json')
            key.write_text(json.dumps({'id': 'disposable-fixture', 'key': 'tskey-auth-DISPOSABLE-NOT-A-REAL-KEY',
                                      'created': now.isoformat(), 'expires': (now + timedelta(hours=24)).isoformat(),
                                      'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': False,
                                                     'preauthorized': True, 'tags': ['tag:guaranine-' + role]}}}}))
            key.chmod(0o600)
            generate(SimpleNamespace(name='test-' + role, role=role, cluster='guaranine',
                     endpoint='https://100.64.0.1:6443', bootstrap_controlplane=False,
                     disk='/dev/vda', confirm_install_disk='/dev/vda',
                     secrets=scratch / 'cluster/secrets.yaml', tailscale_key=key,
                     output=scratch / role,
                     etcd_disk_selector='disk.dev_path == "/dev/vdb"' if role == 'controlplane' else None))
            docs = list(yaml.safe_load_all((scratch / role / 'machine.yaml').read_text()))
            machine = next(doc for doc in docs if doc.get('version') == 'v1alpha1')
            expected_files = {'machine.yaml', 'talosconfig'} if role == 'controlplane' else {'machine.yaml'}
            assert {path.name for path in (scratch / role).iterdir()} == expected_files
            assert machine['machine']['type'] == role
            assert next(doc for doc in docs if doc.get('kind') == 'HostnameConfig')['hostname'] == 'test-' + role
            assert 'TS_HOSTNAME=test-' + role in next(doc for doc in docs if doc.get('kind') == 'ExtensionServiceConfig')['environment']
            assert not any(doc.get('kind') == 'KubePrismConfig' for doc in docs)
            assert any(doc.get('kind') == 'UnattendedInstallConfig' for doc in docs) == (role == 'worker')
            assert next(doc for doc in docs if doc.get('kind') == 'KubeNodeConfig')['nodeIP']['validSubnets'] == ['100.64.0.0/10']
            if role == 'controlplane':
                assert next(doc for doc in docs if doc.get('kind') == 'KubeFlannelCNIConfig')['extraArgs'] == ['--iface=tailscale0']
                pod_api = next(doc for doc in docs if doc.get('kind') == 'NetworkRuleConfig' and doc.get('name') == 'kubernetes-api-from-pods')
                assert pod_api['portSelector'] == {'ports': [6443], 'protocol': 'tcp'}
                assert pod_api['ingress'] == [{'subnet': '10.244.0.0/16'}]
            if role == 'worker':
                assert not any(doc.get('name') == 'kubernetes-api-from-pods' for doc in docs)
                assert not machine['machine']['ca'].get('key')
                assert not machine.get('cluster', {}).get('etcd')
        target = platform.system().lower() + '_' + {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower(), '')
        if target not in ACTIONLINT:
            raise ValueError('Unsupported validation platform')
        archive = scratch / 'actionlint.tar.gz'
        fetch({'url': f'https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_{target}.tar.gz', 'sha256': ACTIONLINT[target]}, archive)
        with tarfile.open(archive) as package:
            executable = scratch / 'actionlint'
            executable.write_bytes(package.extractfile('actionlint').read())
            executable.chmod(0o755)
        subprocess.run([str(executable)], cwd=ROOT, check=True)
    shell = [ROOT / 'guaranine', ROOT / 'scripts/validate.sh']
    subprocess.run(['shellcheck', *map(str, shell)], check=True)
    for path in (ROOT / 'scripts/guaranine.py', ROOT / 'scripts/test-private-images.py'):
        compile(path.read_text(), str(path), 'exec')
    print('Talos roles, Tailscale-only profiles, policy JSON, workflows and shell scripts validated')


if __name__ == '__main__':
    main()

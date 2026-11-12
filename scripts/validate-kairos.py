#!/usr/bin/env python3
"""Validate native Kairos profiles, workflows, shell and public policy without credentials."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request

import yaml
import guaranine as cli

ROOT = Path(__file__).resolve().parents[1]

ACTIONLINT_SHA256 = '023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757'


def binary(url, sha, member, directory):
    archive = directory / (member + '.tar.gz')
    urllib.request.urlretrieve(url, archive)
    if cli.digest(archive) != sha:
        raise ValueError('Upstream validation tool checksum mismatch')
    with tarfile.open(archive) as package:
        matching = [item for item in package if item.isfile() and Path(item.name).name == member]
        if len(matching) != 1:
            raise ValueError('Expected one upstream validation binary')
        target = directory / member
        target.write_bytes(package.extractfile(matching[0]).read())
        target.chmod(0o700)
    return target


def main():
    os.umask(0o077)
    json.loads((ROOT / 'tailscale/policy.json').read_text())
    with tempfile.TemporaryDirectory(prefix='guaranine-validation-') as temporary:
        directory = Path(temporary)
        version = cli.PINS['kairos']
        agent = binary(f'https://github.com/kairos-io/kairos/releases/download/{version}/kairos-{version}-linux-amd64.tar.gz',
                       cli.PINS['agent_sha256'], 'kairos', directory)
        for role in (None, 'worker', 'controlplane'):
            arguments = {} if role is None else dict(name='test-' + role, role=role,
                cluster_endpoint='https://100.64.0.1:6443', token='DISPOSABLE-SERVER' if role == 'controlplane' else 'DISPOSABLE-AGENT',
                agent_token='DISPOSABLE-AGENT')
            config = cli.machine_config('tskey-auth-DISPOSABLE',
                'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBERERERERERERERERERERERERERERERERERERERERER', **arguments)
            path = directory / ((role or 'live') + '.yaml')
            cli.private_write(path, cli.cloud_yaml(config))
            subprocess.run([str(agent), 'agent', '--strict-validation', 'validate', str(path)], check=True)
        subprocess.run([str(agent), 'agent', '--strict-validation', 'validate', str(ROOT / 'kairos/config.yaml')], check=True)
        actionlint = binary('https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_linux_amd64.tar.gz',
                            ACTIONLINT_SHA256, 'actionlint', directory)
        subprocess.run([str(actionlint)], cwd=ROOT, check=True)
        base = yaml.safe_load((ROOT / 'kairos/config.yaml').read_text())
        shell = [ROOT / 'guaranine', ROOT / 'scripts/validate.sh']
        for stage in base['stages'].values():
            for step in stage:
                for file in step.get('files', []):
                    if file['content'].startswith('#!/bin/sh'):
                        path = directory / Path(file['path']).name
                        path.write_text(file['content'])
                        shell.append(path)
        subprocess.run(['shellcheck', *map(str, shell)], check=True)
    for path in (ROOT / 'scripts').rglob('*.py'):
        compile(path.read_text(), str(path), 'exec')
    print('Native Kairos profiles, policy, workflows and shell validated')


if __name__ == '__main__':
    main()

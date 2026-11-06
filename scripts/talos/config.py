#!/usr/bin/env python3
"""Generate private Talos machine configuration with upstream talosctl."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request

import yaml

from common import ROOT, PINS, TAILNET, external, protected, output_directory, private_command, private_write, talosctl


def endpoint(value, role, bootstrap=False):
    parsed = urllib.parse.urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname)
        valid = address in ipaddress.ip_network(TAILNET)
        if bootstrap and role == 'controlplane':
            valid |= address == ipaddress.ip_address('127.0.0.1')
        assert valid and parsed.scheme == 'https' and parsed.port == 6443
        assert not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
        assert parsed.path in ('', '/')
    except (ValueError, TypeError, AssertionError):
        raise ValueError('Use an HTTPS Tailscale IPv4 endpoint on 6443; loopback is only for initial control-plane bootstrap') from None
    return value.rstrip('/')


def auth_record(path, now=None):
    record = json.loads(protected(path).read_text())
    created = datetime.fromisoformat(record['created'].replace('Z', '+00:00'))
    expires = datetime.fromisoformat(record['expires'].replace('Z', '+00:00'))
    now = now or datetime.now(timezone.utc)
    capability = record['capabilities']['devices']['create']
    if not (created.tzinfo and expires.tzinfo and 0 < (expires-created).total_seconds() <= 86400
            and created <= now < expires and capability['reusable'] is False
            and capability['ephemeral'] is False and capability['preauthorized'] is True
            and isinstance(record['key'], str) and record['key'].startswith('tskey-auth-')):
        raise ValueError('Require an unexpired, preauthorized, non-ephemeral, single-use Tailscale key with at most 24 hours lifetime')
    return record


def mint(args):
    """Existing Tailscale API only; the operator credential never enters an image."""
    token = protected(args.api_token).read_text().strip()
    tag = 'tag:guaranine-' + args.role
    request = urllib.request.Request(
        'https://api.tailscale.com/api/v2/tailnet/' + urllib.parse.quote(args.tailnet, safe='') + '/keys',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        data=json.dumps({'capabilities': {'devices': {'create': {
            'reusable': False, 'ephemeral': False, 'preauthorized': True, 'tags': [tag]}}},
            'expirySeconds': 86400, 'description': 'Guaranine ' + args.role + ' enrollment'}).encode())
    # A POST is never automatically repeated after an ambiguous transport failure.
    with urllib.request.urlopen(request, timeout=30) as response:
        record = json.load(response)
    target = external(args.output)
    if not target.parent.is_dir() or target.exists():
        raise ValueError('Use a new output file inside an existing private operator directory')
    private_write(target, json.dumps(record, indent=2) + '\n')
    auth_record(target)
    print('Created a protected 24-hour, single-use enrollment key; no credential printed')


def initialize(args):
    directory = output_directory(args.output)
    private_command([talosctl(), 'gen', 'secrets', '--talos-version', PINS['talos'],
                     '--output-file', str(directory / 'secrets.yaml')], directory)
    (directory / 'secrets.yaml').chmod(0o600)
    print('Created a new private Talos secrets bundle; preserve it for this cluster')


def generate(args):
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', args.name):
        raise ValueError('Use a unique lowercase DNS label with 3–63 characters')
    if not re.fullmatch(r'/dev/(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)', args.disk) or args.confirm_install_disk != args.disk:
        raise ValueError('Select the exact target disk and repeat it with --confirm-install-disk')
    if args.role == 'controlplane' and not args.etcd_disk_selector:
        raise ValueError('The control plane requires an explicit retained data-disk selector for ETCD')
    if args.role == 'worker' and (args.etcd_disk_selector or args.bootstrap_controlplane):
        raise ValueError('Workers cannot run etcd or bootstrap the control plane')
    cluster_endpoint = endpoint(args.endpoint, args.role, args.bootstrap_controlplane)
    secrets = protected(args.secrets)
    record = auth_record(args.tailscale_key)
    required_tag = 'tag:guaranine-' + args.role
    if required_tag not in record['capabilities']['devices']['create'].get('tags', []):
        raise ValueError('The enrollment key must carry the tag for this node role')
    directory = output_directory(args.output)
    command = [talosctl(), 'gen', 'config', args.cluster, cluster_endpoint,
               '--with-secrets', str(secrets), '--talos-version', PINS['talos'],
               '--kubernetes-version', PINS['kubernetes'], '--with-docs=false', '--with-examples=false',
               '--with-cluster-discovery=false', '--install-disk', args.disk,
               '--install-image', f"factory.talos.dev/metal-installer/{PINS['schematic_id']}:{PINS['talos']}",
               '--output-types', args.role + ',talosconfig', '--output', str(directory)]
    private_command(command, directory)
    config = directory / (args.role + '.yaml')
    documents = list(yaml.safe_load_all(config.read_text()))
    # Avoid automatic endpoint selection outside the explicitly selected tailnet.
    documents = [doc for doc in documents if doc.get('kind') != 'KubePrismConfig']
    for doc in documents:
        if doc.get('kind') == 'HostnameConfig':
            doc.clear(); doc.update(apiVersion='v1alpha1', kind='HostnameConfig', hostname=args.name)
    documents.append({'apiVersion': 'v1alpha1', 'kind': 'ExtensionServiceConfig', 'name': 'tailscale',
                      'environment': ['TS_AUTHKEY=' + record['key'], 'TS_HOSTNAME=' + args.name,
                                      'TS_AUTH_ONCE=true', 'TS_USERSPACE=false', 'TS_ACCEPT_DNS=false',
                                      'TS_EXTRA_ARGS=--accept-routes=false']})
    if args.role == 'controlplane':
        documents.append({'apiVersion': 'v1alpha1', 'kind': 'VolumeConfig', 'name': 'ETCD',
                          'provisioning': {'diskSelector': {'match': args.etcd_disk_selector},
                                           'minSize': '2GiB', 'grow': True}})
    config.write_text(yaml.safe_dump_all(documents, sort_keys=False))
    config.chmod(0o600)
    private_command([talosctl(), 'machineconfig', 'patch', str(config),
                     '--patch', '@' + str(ROOT / 'talos/patches/common.yaml'),
                     '--patch', '@' + str(ROOT / 'talos/patches' / (args.role + '.yaml')),
                     '--output', str(directory / 'machine.yaml')], directory)
    private_command([talosctl(), 'validate', '--strict', '--mode', 'metal',
                     '--config', str(directory / 'machine.yaml')], directory)
    for file in directory.iterdir():
        if file.is_file():
            file.chmod(0o600)
    private_write(directory / 'enrollment.json', json.dumps({
        'node': args.name, 'role': args.role, 'tailscale_key_id': record['id'],
        'expires': record['expires'], 'private': True,
        'scope': 'Tailscale key expiry and one tailnet registration; not hardware-bound Talos enrollment'}, indent=2) + '\n')
    print('Generated and schema-validated private machine configuration; never publish this directory')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create a cluster secrets bundle once, outside git')
    init.add_argument('--output', required=True)
    init.set_defaults(action=initialize)
    key = commands.add_parser('mint-key', help='Mint one 24-hour key using the existing Tailscale API')
    key.add_argument('--api-token', required=True)
    key.add_argument('--tailnet', required=True)
    key.add_argument('--role', choices=('controlplane', 'worker'), required=True)
    key.add_argument('--output', required=True)
    key.set_defaults(action=mint)
    node = commands.add_parser('node', help='Generate a private profile for exactly one node')
    for name in ('name', 'endpoint', 'disk', 'confirm-install-disk', 'secrets', 'tailscale-key', 'output'):
        node.add_argument('--' + name, required=True)
    node.add_argument('--role', choices=('controlplane', 'worker'), required=True)
    node.add_argument('--cluster', default='guaranine')
    node.add_argument('--bootstrap-controlplane', action='store_true')
    node.add_argument('--etcd-disk-selector')
    node.set_defaults(action=generate)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        args.action(args)
    except (OSError, ValueError, KeyError) as error:
        # HTTP response bodies and private configuration are never printed.
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()

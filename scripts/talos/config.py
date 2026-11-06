#!/usr/bin/env python3
"""Prepare one machine using the pinned upstream Talos tools."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
import urllib.parse
import urllib.request

import yaml

from common import ROOT, PINS, TAILNET, external, protected, output_directory, private_command, private_write, talosctl
from images import private_image


def operator_directory():
    return external(os.environ.get('GUARANINE_OPERATOR_DIR',
                                  Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'guaranine'))


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


def mint(token, tailnet, role, output):
    """Existing Tailscale API only; the operator credential never enters an image."""
    tag = 'tag:guaranine-' + role
    request = urllib.request.Request(
        'https://api.tailscale.com/api/v2/tailnet/' + urllib.parse.quote(tailnet, safe='') + '/keys',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        data=json.dumps({'capabilities': {'devices': {'create': {
            'reusable': False, 'ephemeral': False, 'preauthorized': True, 'tags': [tag]}}},
            'expirySeconds': 86400, 'description': 'Guaranine ' + role + ' enrollment'}).encode())
    # A POST is never automatically repeated after an ambiguous transport failure.
    with urllib.request.urlopen(request, timeout=30) as response:
        record = json.load(response)
    private_write(output, json.dumps(record, indent=2) + '\n')
    auth_record(output)


def initialize(directory):
    directory = output_directory(directory)
    private_command([talosctl(), 'gen', 'secrets', '--talos-version', PINS['talos'],
                     '--output-file', str(directory / 'secrets.yaml')], directory)
    (directory / 'secrets.yaml').chmod(0o600)
    private_write(directory / 'operator.yaml', yaml.safe_dump({
        'tailnet': 'REPLACE_WITH_YOUR_TAILNET',
        'tailscale_api_token': 'REPLACE_WITH_YOUR_TAILSCALE_API_TOKEN',
        'endpoint': None,
    }, sort_keys=False))
    print(f'Created cluster secrets. Edit {directory / "operator.yaml"}; keep this directory private and backed up.')


def check_node(args):
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', args.name):
        raise ValueError('Use a unique lowercase DNS label with 3–63 characters')
    if not re.fullmatch(r'/dev/(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+)', args.disk) or args.confirm_install_disk != args.disk:
        raise ValueError('Select the exact target disk and repeat it with --confirm-install-disk')
    if args.role == 'controlplane' and not args.etcd_disk_selector:
        raise ValueError('The control plane requires an explicit retained data-disk selector for ETCD')
    if args.role == 'worker' and (args.etcd_disk_selector or args.bootstrap_controlplane):
        raise ValueError('Workers cannot run etcd or bootstrap the control plane')
    return endpoint(args.endpoint, args.role, args.bootstrap_controlplane)


def generate(args):
    cluster_endpoint = check_node(args)
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
    config.unlink()
    for file in directory.iterdir():
        if file.is_file():
            file.chmod(0o600)
    private_write(directory / 'enrollment.json', json.dumps({
        'node': args.name, 'role': args.role, 'tailscale_key_id': record['id'],
        'expires': record['expires'], 'private': True,
        'scope': 'Tailscale key expiry and one tailnet registration; not hardware-bound Talos enrollment'}, indent=2) + '\n')
    return directory / 'machine.yaml'


def prepare(args):
    """One local operation: key, native profile, upstream image. No daemon or API."""
    directory = operator_directory()
    settings = yaml.safe_load(protected(directory / 'operator.yaml').read_text())
    secrets = protected(directory / 'secrets.yaml')
    if not isinstance(settings, dict) or not all(
            isinstance(settings.get(field), str) and settings[field].strip() and not settings[field].startswith('REPLACE_')
            for field in ('tailnet', 'tailscale_api_token')):
        raise ValueError('Fill in tailnet and tailscale_api_token in the private operator.yaml first')
    token = settings['tailscale_api_token'].strip()
    if not token.isascii() or any(character.isspace() for character in token):
        raise ValueError('Use a single ASCII Tailscale API token without embedded whitespace')
    args.endpoint = 'https://127.0.0.1:6443' if args.role == 'controlplane' else settings.get('endpoint')
    args.bootstrap_controlplane = args.role == 'controlplane'
    check_node(args)
    # Check Docker before issuing a real enrollment key or creating node output.
    private_command(['docker', 'info', '--format', '{{.OSType}}'], directory)
    node = directory / 'nodes' / args.name
    if node.exists():
        raise ValueError(f'{node} already exists; reuse its image. Rebuilding requires revoking the old key and removing that node directory first.')
    output_directory(node)
    key = node / 'tailscale-key.json'
    mint(token, settings['tailnet'], args.role, key)
    profile = generate(SimpleNamespace(
        name=args.name, role=args.role, cluster='guaranine', endpoint=args.endpoint,
        bootstrap_controlplane=args.bootstrap_controlplane, disk=args.disk,
        confirm_install_disk=args.confirm_install_disk, secrets=secrets, tailscale_key=key,
        etcd_disk_selector=args.etcd_disk_selector, output=node / 'config'))
    image = private_image(SimpleNamespace(config=profile, format='azure' if args.role == 'controlplane' else 'iso',
                                          output=node / 'image'))
    print(f'Private image ready: {image}')
    print('Keep it private. No Azure resource was created and no laptop was modified.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='guaranine', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create cluster secrets and one private settings file, once')
    init.set_defaults(action=lambda args: initialize(operator_directory()))
    commands.add_parser('talosctl', help='Run the checksum-pinned upstream talosctl', add_help=False)
    for role in ('worker', 'controlplane'):
        node = commands.add_parser(role, help='Prepare one private ISO' if role == 'worker' else 'Prepare the planned Azure control-plane VHD')
        if role == 'worker':
            node.add_argument('name', help='Unique laptop name')
        else:
            node.set_defaults(name='guaranine-controlplane')
        node.add_argument('--disk', required=True, help='Exact whole disk the image will install onto')
        node.add_argument('--confirm-install-disk', required=True, help='Repeat the disk to authorize unattended installation')
        if role == 'controlplane':
            node.add_argument('--etcd-disk-selector', required=True, help='Talos selector for the retained Azure data disk at LUN 0')
        node.set_defaults(action=prepare, role=role, etcd_disk_selector=None)
    os.umask(0o077)
    try:
        argv = sys.argv[1:] if argv is None else argv
        if argv[:1] == ['talosctl']:
            binary = talosctl()
            os.execv(binary, [binary, *argv[1:]])
            return
        args = parser.parse_args(argv)
        args.action(args)
    except yaml.YAMLError:
        parser.exit(1, 'Invalid private YAML; check the operator/configuration file locally.\n')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # HTTP response bodies and private configuration are never printed.
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()

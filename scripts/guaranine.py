#!/usr/bin/env python3
"""Prepare private Talos installers using pinned upstream tools."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / 'talos/versions.json').read_text())
TAILNET = '100.64.0.0/10'
DISK_PATH = r'/dev/(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def external(path):
    path = Path(path).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError('Private inputs and generated outputs must be outside the checkout')
    return path


def protected(path):
    original = Path(path).expanduser()
    info = original.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Private input must be an operator-owned regular file with mode 0600')
    return external(original)


def output_directory(path):
    path = external(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)
    return path


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(data)


def fetch(spec, destination):
    """Only promote a fully downloaded object with the committed digest."""
    destination = Path(destination)
    for attempt in range(4):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
                request = urllib.request.Request(spec['url'], headers={'User-Agent': 'guaranine-talos/0.2', 'Accept': 'application/octet-stream'})
                with urllib.request.urlopen(request, timeout=120) as response:
                    while block := response.read(1024 * 1024):
                        stream.write(block)
            if digest(temporary) != spec['sha256']:
                raise ValueError('Pinned download checksum mismatch')
            temporary.replace(destination)
            return destination
        except (OSError, ValueError):
            if attempt == 3:
                raise
            time.sleep(attempt + 1)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)


def talosctl():
    target = platform.system().lower() + '_' + {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower(), '')
    expected = PINS['talosctl_sha256'].get(target)
    if not expected:
        raise ValueError('Talos tooling supports Linux/macOS AMD64/ARM64')
    override = os.environ.get('GUARANINE_TALOSCTL_BINARY')
    if override:
        binary = Path(override)
        if digest(binary) != expected:
            raise ValueError('The supplied talosctl does not match the committed release digest')
        return str(binary)
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'guaranine' / PINS['talos']
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / ('talosctl-' + target)
    if not binary.exists() or digest(binary) != expected:
        fetch({'url': f"https://github.com/siderolabs/talos/releases/download/{PINS['talos']}/talosctl-{target.replace('_', '-')}", 'sha256': expected}, binary)
    binary.chmod(0o755)
    return str(binary)


def private_command(arguments, directory, *, data=None):
    """Never echo configuration or subprocess error bodies into a terminal/CI log."""
    result = subprocess.run(arguments, input=data, text=True, capture_output=True, timeout=600)
    if result.returncode:
        path = Path(directory) / ('error-' + str(time.time_ns()) + '.log')
        private_write(path, result.stdout + result.stderr)
        raise ValueError(f'Private operation failed; inspect protected diagnostic file {path}')
    return result.stdout


def imager_profile(config, image_format, *, maintenance=False):
    profile = {'baseProfileName': 'azure' if image_format == 'azure' else 'iso',
               'arch': 'amd64', 'version': PINS['talos'].removeprefix('v'),
               'platform': 'azure' if image_format == 'azure' else 'metal',
               'customization': {'embeddedMachineConfiguration': config},
               'input': {'systemExtensions': [{'imageRef': PINS['tailscale_extension']}],
                         'baseInstaller': {'imageRef': PINS['installer_base']}},
               'output': {'kind': 'iso' if image_format == 'iso' else 'image'}}
    if maintenance:
        profile['input']['systemExtensions'] = [{'tarballPath': '/out/tailscale-maintenance.tar'}]
        profile['customization']['extraKernelArgs'] = ['talos.halt_if_installed=1']
    if image_format == 'azure':
        profile['output'].update(imageOptions={'diskFormat': 'vhd', 'diskFormatOptions': 'subformat=fixed,force_size',
                                               'diskSize': 11736711168, 'bootloader': 'dual-boot'}, outFormat='.zst')
    return profile


def maintenance_extension(directory, docker):
    """Run the pinned Tailscale binary with RAM state before installation."""
    container = private_command([*docker, 'create', '--platform', 'linux/amd64', '--entrypoint', '/unused',
                                 PINS['tailscale_extension']], directory.parent).strip()
    original = directory / 'tailscale-upstream.tar'
    try:
        log = directory.parent / 'tailscale-maintenance.log'
        with original.open('xb') as stream, log.open('xb') as errors:
            result = subprocess.run([*docker, 'export', container], stdout=stream,
                                    stderr=errors, timeout=300)
        log.chmod(0o600)
        if result.returncode:
            raise ValueError(f'Could not export the pinned Tailscale extension; inspect {log}')
    finally:
        private_command([*docker, 'rm', container], directory.parent)
    changed = False
    with tarfile.open(original) as source, tarfile.open(directory / 'tailscale-maintenance.tar', 'w') as output:
        for member in source:
            if member.name != 'manifest.yaml' and member.name != 'rootfs' and not member.name.startswith('rootfs/'):
                # Docker's container metadata is not part of the system extension.
                continue
            content = source.extractfile(member) if member.isfile() else None
            if member.name == 'rootfs/usr/local/etc/containers/tailscale.yaml':
                spec = yaml.safe_load(content.read())
                if spec['depends'].count({'service': 'cri'}) != 1:
                    raise ValueError('The pinned Tailscale service dependency changed; review its maintenance support')
                spec['depends'].remove({'service': 'cri'})
                environment = spec['container']['environment']
                if environment.count('TS_STATE_DIR=/var/lib/tailscale') != 1:
                    raise ValueError('The pinned Tailscale state directory changed; review its maintenance support')
                environment[environment.index('TS_STATE_DIR=/var/lib/tailscale')] = 'TS_STATE_DIR=/var/run/tailscale'
                mounts = spec['container']['mounts']
                state_mounts = [mount for mount in mounts if mount['source'] == '/var/lib/tailscale']
                if len(state_mounts) != 1 or state_mounts[0]['destination'] != '/var/lib/tailscale':
                    raise ValueError('The pinned Tailscale state mount changed; review its maintenance support')
                mounts.remove(state_mounts[0])
                data = yaml.safe_dump(spec, sort_keys=False).encode()
                member.size = len(data)
                content = io.BytesIO(data)
                changed = True
            output.addfile(member, content)
            if content is not None:
                content.close()
    if not changed:
        raise ValueError('The pinned extension did not contain its expected Tailscale service')
    original.unlink()


def private_image(args, *, use_sudo=False, maintenance=False):
    config = protected(args.config)
    documents = list(yaml.safe_load_all(config.read_text()))
    machine = next((doc for doc in documents if doc.get('version') == 'v1alpha1'), None)
    if maintenance:
        allowed = {'HostnameConfig', 'ExtensionServiceConfig', 'NetworkDefaultActionConfig', 'NetworkRuleConfig'}
        if args.format != 'iso' or any(doc.get('kind') not in allowed for doc in documents):
            raise ValueError('The shared ISO must contain only maintenance networking, never installation or cluster authority')
    elif not machine or machine.get('machine', {}).get('type') != 'controlplane':
        raise ValueError('A complete private Talos machine configuration is required')
    elif args.format != 'azure':
        raise ValueError('Worker installations use the shared maintenance ISO')
    if not any(doc.get('kind') == 'ExtensionServiceConfig' and doc.get('name') == 'tailscale' for doc in documents):
        raise ValueError('The image must include the Tailscale enrollment configuration')
    directory = output_directory(args.output)
    private_command([talosctl(), 'validate', '--strict', '--mode', 'metal', '--config', str(config)], directory)
    profile = yaml.safe_dump(imager_profile(config.read_text(), args.format, maintenance=maintenance), sort_keys=False)
    docker = (['sudo', '--'] if use_sudo else []) + ['docker']
    # Profile/credentials go through stdin, never shell arguments, environment,
    # public Image Factory, a Docker build context, or a repository file. Stage
    # large disk files on operator storage; tar output keeps final files owned
    # by the operator instead of the privileged container.
    archive = directory / 'imager-output.tar'
    log = directory / 'imager.log'
    with (tempfile.TemporaryDirectory(prefix='imager-', dir=directory) as scratch,
          archive.open('xb') as stream, log.open('xb') as errors):
        if maintenance:
            maintenance_extension(Path(scratch), docker)
        result = subprocess.run([*docker, 'run', '--rm', '--privileged', '--platform', 'linux/amd64', '-i',
                                 '--mount', f'type=bind,source={scratch},target=/out', PINS['imager'], '-', '--tar-to-stdout'],
                                input=profile.encode(), stdout=stream, stderr=errors, timeout=900)
    archive.chmod(0o600); log.chmod(0o600)
    if result.returncode:
        raise ValueError(f'Private imager failed; inspect protected diagnostics in {log}')
    artifacts = []
    with tarfile.open(archive) as package:
        for member in package:
            suffix = '.iso' if args.format == 'iso' else '.vhd.zst'
            if not member.isfile() or not member.name.endswith(suffix):
                continue
            if Path(member.name).name != member.name or member.size <= 0:
                raise ValueError('Unsafe image archive member')
            target = directory / member.name
            with package.extractfile(member) as source, target.open('xb') as destination:
                while block := source.read(1024 * 1024):
                    destination.write(block)
            target.chmod(0o600)
            artifacts.append(target)
    if len(artifacts) != 1:
        raise ValueError('The upstream imager must produce exactly one image of the requested format')
    archive.unlink()
    print('Built a private Talos machine image; keep the entire output directory private')
    return artifacts[0]


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


def auth_record(path, now=None, *, reusable=False):
    record = json.loads(protected(path).read_text())
    created = datetime.fromisoformat(record['created'].replace('Z', '+00:00'))
    expires = datetime.fromisoformat(record['expires'].replace('Z', '+00:00'))
    now = now or datetime.now(timezone.utc)
    capability = record['capabilities']['devices']['create']
    if not (created.tzinfo and expires.tzinfo and 0 < (expires-created).total_seconds() <= 86400
            and created <= now < expires and capability['reusable'] is reusable
            and capability['ephemeral'] is False and capability['preauthorized'] is True
            and isinstance(record['key'], str) and record['key'].startswith('tskey-auth-')):
        use = 'reusable' if reusable else 'single-use'
        raise ValueError(f'Require an unexpired, preauthorized, non-ephemeral, {use} Tailscale key with at most 24 hours lifetime')
    return record


def mint(token, tailnet, role, output, *, reusable=False):
    """Existing Tailscale API only; the operator credential never enters an image."""
    tag = 'tag:guaranine-' + role
    request = urllib.request.Request(
        'https://api.tailscale.com/api/v2/tailnet/' + urllib.parse.quote(tailnet, safe='') + '/keys',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        data=json.dumps({'capabilities': {'devices': {'create': {
            'reusable': reusable, 'ephemeral': False, 'preauthorized': True, 'tags': [tag]}}},
            'expirySeconds': 86400, 'description': 'Guaranine ' + role + ' enrollment'}).encode())
    # A POST is never automatically repeated after an ambiguous transport failure.
    with urllib.request.urlopen(request, timeout=30) as response:
        record = json.load(response)
    private_write(output, json.dumps(record, indent=2) + '\n')
    auth_record(output, reusable=reusable)


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


def check_name(name):
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', name):
        raise ValueError('Use a unique lowercase DNS label with 3–63 characters')


def check_node(args):
    check_name(args.name)
    if not re.fullmatch(DISK_PATH, args.disk) or args.confirm_install_disk != args.disk:
        raise ValueError('Select the exact target disk and repeat it with --confirm-install-disk')
    if args.role == 'controlplane' and not args.etcd_disk_selector:
        raise ValueError('The control plane requires an explicit retained data-disk selector for ETCD')
    if args.role == 'worker' and (args.etcd_disk_selector or args.bootstrap_controlplane):
        raise ValueError('Workers cannot run etcd or bootstrap the control plane')
    return endpoint(args.endpoint, args.role, args.bootstrap_controlplane)


def generate(args, *, reusable=False):
    cluster_endpoint = check_node(args)
    secrets = protected(args.secrets)
    record = auth_record(args.tailscale_key, reusable=reusable)
    required_tag = 'tag:guaranine-' + args.role
    if required_tag not in record['capabilities']['devices']['create'].get('tags', []):
        raise ValueError('The enrollment key must carry the tag for this node role')
    directory = output_directory(args.output)
    config = directory / (args.role + '.yaml')
    # Only the control-plane profile needs an operator administration context.
    output_types = 'controlplane,talosconfig' if args.role == 'controlplane' else 'worker'
    output = directory if args.role == 'controlplane' else config
    command = [talosctl(), 'gen', 'config', args.cluster, cluster_endpoint,
               '--with-secrets', str(secrets), '--talos-version', PINS['talos'],
               '--kubernetes-version', PINS['kubernetes'], '--with-docs=false', '--with-examples=false',
               '--with-cluster-discovery=false', '--install-disk', args.disk,
               '--install-image', f"factory.talos.dev/metal-installer/{PINS['schematic_id']}:{PINS['talos']}",
               '--output-types', output_types, '--output', str(output)]
    private_command(command, directory)
    documents = list(yaml.safe_load_all(config.read_text()))
    # Avoid automatic endpoint selection outside the explicitly selected tailnet.
    documents = [doc for doc in documents if doc.get('kind') != 'KubePrismConfig']
    if args.role == 'controlplane':
        # Azure boots a prepared disk. Never reinstall onto a disk name which
        # Azure may assign to the retained etcd volume on a later boot.
        documents = [doc for doc in documents if doc.get('kind') != 'UnattendedInstallConfig']
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
    patches = ['--patch', '@' + str(ROOT / 'talos/patches/common.yaml')]
    if args.role == 'controlplane':
        patches += ['--patch', '@' + str(ROOT / 'talos/patches/controlplane.yaml')]
    private_command([talosctl(), 'machineconfig', 'patch', str(config), *patches,
                     '--output', str(directory / 'machine.yaml')], directory)
    private_command([talosctl(), 'validate', '--strict', '--mode', 'metal',
                     '--config', str(directory / 'machine.yaml')], directory)
    config.unlink()
    for file in directory.iterdir():
        if file.is_file():
            file.chmod(0o600)
    return directory / 'machine.yaml'


def operator_settings():
    directory = operator_directory()
    settings = yaml.safe_load(protected(directory / 'operator.yaml').read_text())
    if not isinstance(settings, dict) or not all(
            isinstance(settings.get(field), str) and settings[field].strip() and not settings[field].startswith('REPLACE_')
            for field in ('tailnet', 'tailscale_api_token')):
        raise ValueError('Fill in tailnet and tailscale_api_token in the private operator.yaml first')
    token = settings['tailscale_api_token'].strip()
    if not token.isascii() or any(character.isspace() for character in token):
        raise ValueError('Use a single ASCII Tailscale API token without embedded whitespace')
    return settings


def boot_config(key):
    return yaml.safe_dump_all([
        {'apiVersion': 'v1alpha1', 'kind': 'HostnameConfig', 'auto': 'stable'},
        {'apiVersion': 'v1alpha1', 'kind': 'ExtensionServiceConfig', 'name': 'tailscale',
         'environment': ['TS_AUTHKEY=' + key, 'TS_AUTH_ONCE=true', 'TS_USERSPACE=false',
                         'TS_ACCEPT_DNS=false', 'TS_EXTRA_ARGS=--accept-routes=false']},
        {'apiVersion': 'v1alpha1', 'kind': 'NetworkDefaultActionConfig', 'ingress': 'block'},
        {'apiVersion': 'v1alpha1', 'kind': 'NetworkRuleConfig', 'name': 'maintenance-over-tailscale',
         'portSelector': {'ports': [50000], 'protocol': 'tcp'}, 'ingress': [{'subnet': TAILNET}]},
    ], sort_keys=False)


def prepare_iso(_args):
    directory = operator_directory()
    settings = operator_settings()
    installer = directory / 'installer'
    if installer.exists():
        raise ValueError(f'{installer} already exists; reuse its ISO. Revoke the batch key before removing it to build another ISO.')
    private_command(['sudo', '--', 'docker', 'info', '--format', '{{.OSType}}'], directory)
    output_directory(installer)
    key = installer / 'tailscale-key.json'
    mint(settings['tailscale_api_token'].strip(), settings['tailnet'], 'worker', key, reusable=True)
    record = auth_record(key, reusable=True)
    config = installer / 'boot.yaml'
    private_write(config, boot_config(record['key']))
    image = private_image(SimpleNamespace(config=config, format='iso', output=installer / 'image'),
                          use_sudo=True, maintenance=True)
    print(f'Private shared ISO ready: {image}')
    print(f'New Tailscale registrations expire at {record["expires"]}. Booting this ISO does not authorize disk installation.')
    print('After booting a laptop, run ./guaranine worker on the operator computer to choose its name and disk.')


def read_disks(connection, directory):
    raw = private_command([talosctl(), 'get', 'disks', *connection, '--output', 'json'], directory)
    disks = []
    # talosctl streams one JSON object per resource, without an enclosing array.
    decoder = json.JSONDecoder()
    while raw.strip():
        resource, end = decoder.raw_decode(raw.lstrip())
        raw = raw.lstrip()[end:]
        spec = resource['spec']
        if re.fullmatch(DISK_PATH, spec.get('dev_path', '')) and not spec.get('readonly', True) and not spec.get('cdrom', True):
            disks.append(spec)
    return sorted(disks, key=lambda disk: disk['dev_path'])


def install_worker(_args):
    directory = operator_directory()
    settings = operator_settings()
    cluster_endpoint = endpoint(settings.get('endpoint'), 'worker')
    secrets = protected(directory / 'secrets.yaml')
    key = directory / 'installer/tailscale-key.json'
    auth_record(key, reusable=True)
    address = input('Laptop Tailscale IPv4 address: ').strip()
    if ipaddress.ip_address(address) not in ipaddress.ip_network(TAILNET):
        raise ValueError('Use the target laptop\'s Tailscale IPv4 address')
    fingerprint = input('Talos certificate fingerprint from that laptop\'s screen: ').strip()
    if not fingerprint:
        raise ValueError('Copy the certificate fingerprint from the target laptop before installing')
    name = input('Worker name: ').strip()
    check_name(name)
    node = directory / 'nodes' / name
    if node.exists():
        raise ValueError(f'{node} already exists; use a new worker name')
    connection = ['--insecure', '--nodes', address, '--cert-fingerprint', fingerprint]
    disks = read_disks(connection, directory)
    if not disks:
        raise ValueError('The laptop reported no supported writable installation disks')
    for number, disk in enumerate(disks, 1):
        print(f'{number}. {disk.get("model", "Unknown model")} | {disk.get("pretty_size", disk["size"])} | '
              f'{disk.get("serial", "No serial")} | {disk.get("transport", "Unknown transport")} | {disk["dev_path"]}')
    choice = input('Installation disk number (no default): ').strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(disks):
        raise ValueError('Select one of the listed disk numbers; nothing was installed')
    selected = disks[int(choice) - 1]
    disk = selected['dev_path']
    confirmation = f'ERASE {name} {disk}'
    print(f'Install worker {name} on {address}; ALL DATA on {disk} will be erased.')
    if input(f'Type {confirmation} to install, or anything else to cancel: ').strip() != confirmation:
        raise ValueError('Installation cancelled; no configuration was applied')
    if selected not in read_disks(connection, directory):
        raise ValueError('The selected disk changed; inspect the laptop again before installing')
    auth_record(key, reusable=True)
    output_directory(node)
    profile = generate(SimpleNamespace(
        name=name, role='worker', cluster='guaranine', endpoint=cluster_endpoint,
        bootstrap_controlplane=False, disk=disk, confirm_install_disk=disk,
        secrets=secrets, tailscale_key=key, etcd_disk_selector=None, output=node / 'config'), reusable=True)
    auth_record(key, reusable=True)
    private_command([talosctl(), 'apply-config', *connection, '--file', str(profile)], node)
    print(f'Installation configuration sent for {name}. The laptop will install and reboot; remove the USB when it reboots.')
    print(f'Its Tailscale address may change. Verify kubectl get node {name} reports Ready before using it.')


def prepare(args):
    """Prepare the one control-plane VHD with the pinned upstream tools."""
    directory = operator_directory()
    settings = operator_settings()
    secrets = protected(directory / 'secrets.yaml')
    args.endpoint = 'https://127.0.0.1:6443'
    args.bootstrap_controlplane = True
    check_node(args)
    node = directory / 'nodes' / args.name
    if node.exists():
        raise ValueError(f'{node} already exists; reuse its image. Rebuilding requires revoking the old key and removing that node directory first.')
    # Elevate only Docker, before issuing a key or creating private node output.
    private_command(['sudo', '--', 'docker', 'info', '--format', '{{.OSType}}'], directory)
    output_directory(node)
    key = node / 'tailscale-key.json'
    mint(settings['tailscale_api_token'].strip(), settings['tailnet'], args.role, key)
    profile = generate(SimpleNamespace(
        name=args.name, role=args.role, cluster='guaranine', endpoint=args.endpoint,
        bootstrap_controlplane=args.bootstrap_controlplane, disk=args.disk,
        confirm_install_disk=args.confirm_install_disk, secrets=secrets, tailscale_key=key,
        etcd_disk_selector=args.etcd_disk_selector, output=node / 'config'))
    image = private_image(SimpleNamespace(config=profile, format='azure',
                                          output=node / 'image'), use_sudo=True)
    print(f'Private image ready: {image}')
    print('Keep it private. No Azure resource was created and no laptop was modified.')


def build_controlplane(args):
    """Finish a prepared control-plane image; elevate only the upstream imager."""
    node = operator_directory() / 'nodes/guaranine-controlplane'
    if (node / 'image').exists():
        raise ValueError(f'{node / "image"} already exists; inspect its image and imager.log. '
                         'This command builds an image; Azure VM provisioning is separate.')
    auth_record(node / 'tailscale-key.json')
    print('Building the private control-plane image; sudo authorizes only the Docker build.', flush=True)
    image = private_image(SimpleNamespace(config=node / 'config/machine.yaml', format='azure',
                                          output=node / 'image'), use_sudo=True)
    print(f'Private image ready: {image}')
    print('Keep it private. No Azure resource was created.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='guaranine', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create cluster secrets and one private settings file, once')
    init.set_defaults(action=lambda args: initialize(operator_directory()))
    build = commands.add_parser('build-controlplane', help='Build the prepared control-plane VHD; sudo authorizes Docker only')
    build.set_defaults(action=build_controlplane)
    commands.add_parser('iso', help='Build one private maintenance ISO for a 24-hour laptop batch').set_defaults(action=prepare_iso)
    commands.add_parser('worker', help='Choose a booted laptop\'s worker name and installation disk').set_defaults(action=install_worker)
    commands.add_parser('talosctl', help='Run the checksum-pinned upstream talosctl', add_help=False)
    node = commands.add_parser('controlplane', help='Prepare the planned Azure control-plane VHD')
    node.add_argument('--disk', required=True, help='Exact whole OS disk')
    node.add_argument('--confirm-install-disk', required=True, help='Repeat the disk to confirm the choice')
    node.add_argument('--etcd-disk-selector', required=True, help='Talos selector for the retained Azure data disk at LUN 0')
    node.set_defaults(action=prepare, role='controlplane', name='guaranine-controlplane')
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
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, 'Cancelled. If configuration was already sent, check the laptop before retrying.\n')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # HTTP response bodies and private configuration are never printed.
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()

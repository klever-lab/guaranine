#!/usr/bin/env python3
"""Build private Kairos media and install K3s workers over Tailscale."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / 'kairos/versions.json').read_text())
TAILNET = ipaddress.ip_network('100.64.0.0/10')
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


@contextmanager
def progress(message):
    print(message, flush=True)
    finished = threading.Event()
    def report():
        elapsed = 0
        while not finished.wait(15):
            elapsed += 15
            print(f'{message} ({elapsed}s elapsed)', flush=True)
    thread = threading.Thread(target=report, daemon=True)
    thread.start()
    try:
        yield
    finally:
        finished.set()
        thread.join()


def private_command(arguments, directory, *, data=None, timeout=600):
    """Never echo configuration or subprocess error bodies into a terminal/CI log."""
    result = subprocess.run(arguments, input=data, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        path = Path(directory) / ('error-' + str(time.time_ns()) + '.log')
        private_write(path, result.stdout + result.stderr)
        raise ValueError(f'Private operation failed; inspect protected diagnostic file {path}')
    return result.stdout


def auth_record(path, now=None, *, reusable=False):
    record = json.loads(protected(path).read_text())
    created = datetime.fromisoformat(record['created'].replace('Z', '+00:00'))
    expires = datetime.fromisoformat(record['expires'].replace('Z', '+00:00'))
    now = now or datetime.now(timezone.utc)
    capability = record['capabilities']['devices']['create']
    # Tolerate small API/operator clock differences without extending expiry.
    if not (created.tzinfo and expires.tzinfo and 0 < (expires-created).total_seconds() <= 86400
            and created <= now + timedelta(seconds=5) and now < expires and capability['reusable'] is reusable
            and capability['ephemeral'] is False and capability['preauthorized'] is True
            and capability.get('tags') == ['tag:guaranine-' + ('worker' if reusable else 'controlplane')]
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


def check_name(name):
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', name):
        raise ValueError('Use a unique lowercase DNS label with 3–63 characters')


def usb_disks(image):
    """Offer plain USB disks, excluding system mounts and the ISO's own disk."""
    source = image.stat()
    source_device = f'{os.major(source.st_dev)}:{os.minor(source.st_dev)}'
    result = subprocess.run(['lsblk', '--json', '--bytes', '--paths', '--tree', '--output',
                             'PATH,TYPE,TRAN,RO,SIZE,MODEL,SERIAL,LABEL,MOUNTPOINTS,MAJ:MIN'],
                            check=True, capture_output=True, text=True, timeout=30)
    disks = []
    for disk in json.loads(result.stdout)['blockdevices']:
        children = disk.get('children', [])
        family = [disk, *children]
        if disk['type'] != 'disk' or disk.get('tran') != 'usb' or disk.get('ro') or disk['size'] < source.st_size:
            continue
        if any(child['type'] != 'part' or child.get('children') for child in children):
            continue
        if any(member['maj:min'] == source_device for member in family):
            continue
        mounts = [mount for member in family for mount in member.get('mountpoints', []) if mount]
        if any(not any(mount == root or mount.startswith(root + '/') for root in ('/media', '/run/media', '/mnt'))
               for mount in mounts):
            continue
        disks.append(disk)
    return sorted(disks, key=lambda disk: disk['path'])


def flash_iso(image):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    image = protected(image)
    try:
        disks = usb_disks(image)
        if not disks:
            print('No suitable USB drives found; the ISO is saved for later flashing.')
            return False
        print('\nFlash the ISO to a USB drive:')
        print('0. Skip flashing (default)')
        for number, disk in enumerate(disks, 1):
            labels = ', '.join(member['label'] for member in [disk, *disk.get('children', [])] if member.get('label'))
            print(f'{number}. {disk["path"]} | {disk.get("model") or "Unknown model"} | '
                  f'{disk["size"] / 2**30:.1f} GiB | {disk.get("serial") or "No serial"} | {labels or "No label"}')
        choice = input('USB drive number (Enter to skip): ').strip()
        if choice in ('', '0'):
            return False
        if not choice.isdecimal() or not 1 <= int(choice) <= len(disks):
            raise ValueError('Select a listed USB drive; nothing was flashed')
        selected = disks[int(choice) - 1]
        path = selected['path']
        confirmation = 'ERASE'
        print(f'ALL DATA on {path} will be erased and replaced with {image.name}.')
        answer = input('Type ERASE to write this USB (Enter to cancel): ').strip().upper()
        if answer != confirmation:
            if answer:
                print(f'Confirmation did not match. The required text is: {confirmation}')
            print(f'Flashing cancelled. Nothing was written to {path}.')
            return False
        if selected not in usb_disks(image):
            raise ValueError('The selected USB drive changed; nothing was flashed')
        for member in [*selected.get('children', []), selected]:
            if any(member.get('mountpoints', [])):
                subprocess.run(['sudo', '--', 'umount', '--all-targets', '--', member['path']], check=True, timeout=30)
        current = next((disk for disk in usb_disks(image) if disk['path'] == path), {})
        if (any(current.get(field) != selected.get(field) for field in ('path', 'maj:min', 'size', 'model', 'serial'))
                or any(mount for member in [current, *current.get('children', [])]
                       for mount in member.get('mountpoints', []))):
            raise ValueError('The selected USB drive changed or is still mounted; nothing was flashed')
        print(f'Writing {image.stat().st_size / 2**20:.1f} MiB to {path}; progress appears below.', flush=True)
        print('Wait for the "Flashed" success message before removing the USB.', flush=True)
        subprocess.run(['sudo', '--', 'dd', f'if={image}', f'of={path}', 'bs=4M', 'oflag=dsync',
                        'status=progress', 'conv=fsync,nocreat'],
                       check=True, timeout=900)
        print(f'\nFlashed {image.name} to {path}. You can now remove the USB drive.')
        return True
    except (EOFError, KeyboardInterrupt):
        print(f'\nFlashing cancelled; the ISO is saved at {image}.')
        return False


def operator_directory():
    return external(os.environ.get('GUARANINE_OPERATOR_DIR',
        Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'guaranine-kairos'))


def initialize(directory):
    directory = output_directory(directory)
    private_command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'guaranine operator',
                     '-f', str(directory / 'ssh_key')], directory)
    private_write(directory / 'server-token', secrets.token_hex(32) + '\n')
    private_write(directory / 'agent-token', secrets.token_hex(32) + '\n')
    private_write(directory / 'operator.yaml', yaml.safe_dump({
        'tailnet': 'REPLACE_WITH_YOUR_TAILNET',
        'tailscale_api_token': 'REPLACE_WITH_YOUR_TAILSCALE_API_TOKEN',
        'endpoint': None,
    }, sort_keys=False))
    print(f'Created private Kairos operator files. Edit {directory / "operator.yaml"}.')


def operator_settings():
    directory = operator_directory()
    settings = yaml.safe_load(protected(directory / 'operator.yaml').read_text())
    if not isinstance(settings, dict) or not all(
            isinstance(settings.get(field), str) and settings[field].strip()
            and not settings[field].startswith('REPLACE_') for field in ('tailnet', 'tailscale_api_token')):
        raise ValueError('Fill in tailnet and tailscale_api_token in the private operator.yaml first')
    token = settings['tailscale_api_token'].strip()
    if not token.isascii() or any(character.isspace() for character in token):
        raise ValueError('Use a single ASCII Tailscale API token without embedded whitespace')
    protected(directory / 'ssh_key')
    return settings


def tailnet_ip(value):
    try:
        address = ipaddress.ip_address(value)
        if address.version == 4 and address in TAILNET:
            return str(address)
    except ValueError:
        pass
    raise ValueError('Use a Tailscale IPv4 address in 100.64.0.0/10')


def endpoint(value):
    parsed = urllib.parse.urlsplit(value or '')
    try:
        tailnet_ip(parsed.hostname)
        if (parsed.scheme == 'https' and parsed.port == 6443 and parsed.path in ('', '/')
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment):
            return value.rstrip('/')
    except (ValueError, TypeError):
        pass
    raise ValueError('Set endpoint to https://CONTROL_PLANE_TAILSCALE_IPV4:6443')


def config_file(path, content, permissions=0o600):
    return {'path': path, 'content': content, 'permissions': permissions, 'owner': 0, 'group': 0}


def operator_wifi(directory):
    """Read one active Wi-Fi connection; never copy hardware bindings or IP settings."""
    def nmcli(*args, secrets=False):
        command = ['nmcli', '--escape', 'no']
        if secrets:
            command.append('--show-secrets')
        return private_command([*command, *args], directory).removesuffix('\n')
    active = nmcli('--terse', '--fields', 'UUID,TYPE', 'connection', 'show', '--active')
    connections = [line.split(':', 1)[0] for line in active.splitlines() if line.endswith(':802-11-wireless')]
    if len(connections) != 1:
        raise ValueError('Connect the operator to exactly one Wi-Fi network before building laptop media')
    connection = connections[0]
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', connection):
        raise ValueError('Invalid active Wi-Fi connection UUID')
    def setting(field, *, secret=False):
        return nmcli('--get-values', field, 'connection', 'show', 'uuid', connection, secrets=secret)
    ssid = setting('802-11-wireless.ssid').encode()
    if not 1 <= len(ssid) <= 32:
        raise ValueError('The active Wi-Fi SSID must contain 1–32 bytes')
    mode = setting('802-11-wireless.mode')
    security = setting('802-11-wireless-security.key-mgmt')
    if mode not in ('infrastructure', '') or security not in ('', 'wpa-psk', 'sae'):
        raise ValueError('Use an infrastructure Wi-Fi network with open, WPA personal or WPA3 personal authentication')
    lines = ['update_config=0', 'p2p_disabled=1', 'network={', '    ssid=' + ssid.hex()]
    if not security:
        lines.append('    key_mgmt=NONE')
    else:
        password = setting('802-11-wireless-security.psk', secret=True)
        if security == 'sae':
            if not 1 <= len(password.encode()) <= 63 or any(ord(c) < 32 for c in password):
                raise ValueError('The saved WPA3 password is unavailable or unsupported; no media was built')
            escaped = password.replace('\\', '\\\\').replace('"', '\\"')
            lines += ['    key_mgmt=SAE', '    ieee80211w=2', '    sae_password="' + escaped + '"']
        else:
            if re.fullmatch(r'[0-9a-fA-F]{64}', password):
                psk = password.lower()
            elif 8 <= len(password.encode()) <= 63:
                psk = hashlib.pbkdf2_hmac('sha1', password.encode(), ssid, 4096, 32).hex()
            else:
                raise ValueError('The saved Wi-Fi password is unavailable; no media was built')
            lines += ['    key_mgmt=WPA-PSK', '    proto=RSN', '    psk=' + psk]
    return '\n'.join([*lines, '}']) + '\n'


def role_config(role):
    if role not in ('worker', 'controlplane'):
        raise ValueError('Use the worker or controlplane Kairos profile')
    return yaml.safe_load((ROOT / 'kairos' / (role + '.yaml')).read_text())


def machine_config(key, public_key, *, name=None, role=None, cluster_endpoint=None, token=None, agent_token=None, wifi=None):
    """Add private inputs to a native role profile; the shared OS config stays in the image."""
    config = role_config(role) if role else {'stages': {'initramfs': [], 'boot': []}}
    name = name or config.get('hostname')
    if name:
        check_name(name)
        config['hostname'] = name
    config['ssh_authorized_keys'] = [public_key.strip()]
    files = [config_file('/etc/guaranine/tailscale-key', key + '\n')]
    if wifi:
        files.append(config_file('/etc/guaranine/wifi.conf', wifi))
    if role:
        if not name or not token:
            raise ValueError('A named worker or control plane needs its own K3s token')
        files.append(config_file('/etc/rancher/k3s/token', token.strip() + '\n'))
        if role == 'worker':
            files.append(config_file('/etc/rancher/k3s/config.yaml.d/90-join.yaml',
                                     yaml.safe_dump({'server': endpoint(cluster_endpoint)})))
        else:
            if not agent_token or agent_token.strip() == token.strip():
                raise ValueError('The control plane needs a separate agent token')
            files.append(config_file('/etc/rancher/k3s/agent-token', agent_token.strip() + '\n'))
            files.append(config_file('/etc/rancher/k3s/network-policy.yaml',
                                     (ROOT / 'kubernetes/network-policy.yaml').read_text()))
    config['stages']['initramfs'].append({'name': 'Configure Guaranine private inputs', 'files': files})
    return config


def cloud_yaml(config):
    return '#cloud-config\n' + yaml.safe_dump(config, sort_keys=False)


def build_base(directory):
    """Only public inputs enter the Docker build context and its cache."""
    identity = hashlib.sha256(json.dumps(PINS, sort_keys=True).encode()
                             + (ROOT / 'kairos/Dockerfile').read_bytes()
                             + (ROOT / 'kairos/config.yaml').read_bytes()).hexdigest()[:16]
    image = 'guaranine-kairos:' + identity
    args = {'K3S_VERSION': PINS['k3s'], 'K3S_SHA256': PINS['k3s_sha256']}
    command = ['sudo', '--', 'docker', 'build', '--platform', 'linux/amd64', '--tag', image]
    for key, value in args.items():
        command += ['--build-arg', f'{key}={value}']
    with progress('Building the shared Kairos/Hadron base with K3s and Tailscale…'):
        private_command([*command, str(ROOT / 'kairos')], directory, timeout=1800)
    return image


def build_image(config, image_format, directory, source):
    """Run the pinned upstream builder offline. Private config only enters stdin."""
    live_cmdline = role_config('worker')['install']['grub_options']['extra_cmdline']
    directory = output_directory(directory)
    log = directory / 'build.log'
    # Stage private inputs and disk files on protected operator storage.
    script = '''set -eu
umask 077
trap 'chown -R "$3:$4" /work' EXIT
mkdir -p /work/images /work/tmp
export TMPDIR=/work/tmp
# Private inputs are protected; OS directories need their standard permissions.
umask 022
if [ "$1" = iso ]; then
  auroraboot build-iso --arch amd64 --cloud-config - --output /work/images --override-name guaranine-kairos --extend-live-cmdline "$5" "$2"
else
  auroraboot --cloud-config - --set "container_image=$2" --set state_dir=/work/images /auroraboot.yaml
fi
'''
    container = 'guaranine-build-' + secrets.token_hex(8)
    with (progress(f'Building private Kairos {image_format.upper()}…'),
          tempfile.TemporaryDirectory(prefix='build-', dir=directory, ignore_cleanup_errors=True) as scratch,
          log.open('xb') as errors):
        log.chmod(0o600)
        try:
            result = subprocess.run(['sudo', '--', 'docker', 'run', '--rm', '--privileged', '--log-driver', 'none', '--name', container,
                '--platform', 'linux/amd64', '-i', '--mount', f'type=bind,source={scratch},target=/work', '--mount',
                'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock',
                '--mount', f'type=bind,source={ROOT / "kairos/auroraboot.yaml"},target=/auroraboot.yaml,readonly',
                '--entrypoint', '/bin/bash', PINS['auroraboot'], '-ec', script, 'guaranine-builder',
                image_format, 'oci:' + source, str(os.getuid()), str(os.getgid()), live_cmdline], input=cloud_yaml(config).encode(), stdout=errors,
                stderr=subprocess.STDOUT, timeout=1800)
        finally:
            subprocess.run(['sudo', '--', 'docker', 'rm', '--force', container],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode:
            raise ValueError(f'Kairos image build failed (exit {result.returncode}); inspect protected diagnostics in {log}')
        suffix = '.iso' if image_format == 'iso' else '.vhd'
        images = list((Path(scratch) / 'images').glob('*' + suffix))
        if len(images) != 1 or images[0].is_symlink() or not images[0].is_file() or images[0].stat().st_size <= 0:
            raise ValueError('The upstream builder must return exactly one nonempty regular image')
        images[0].chmod(0o600)
        image = images[0].rename(directory / ('guaranine-kairos' + suffix))
    return image


def iso_source():
    """Identify the checkout and actual public inputs, including local edits."""
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    sources = hashlib.sha256()
    paths = [ROOT / 'guaranine', ROOT / 'scripts/guaranine.py', ROOT / 'requirements.txt',
             *sorted((ROOT / 'kairos').rglob('*'))]
    for path in paths:
        if path.is_file():
            sources.update(str(path.relative_to(ROOT)).encode() + b'\0' + bytes.fromhex(digest(path)))
    return {'commit': commit, 'sources_sha256': sources.hexdigest()}


def record_iso_source(image, source):
    if iso_source() != source:
        raise ValueError('The source changed while building the ISO; rerun ./guaranine iso before flashing')
    private_write(image.parent / 'source.json', json.dumps(source, indent=2) + '\n')


def prepare_iso(_args):
    directory = operator_directory()
    settings = operator_settings()
    installer = directory / 'installer'
    key, config_path = installer / 'tailscale-key.json', installer / 'boot.yaml'
    image = installer / 'image/guaranine-kairos.iso'
    current_source = iso_source()
    if image.exists():
        record = auth_record(key, reusable=True)
        protected(image)
        saved_source = None
        metadata = image.parent / 'source.json'
        if metadata.exists():
            try:
                saved_source = json.loads(protected(metadata).read_text())
            except (json.JSONDecodeError, UnicodeError):
                pass
        if getattr(_args, 'rebuild', False) or saved_source != current_source:
            print('Rebuilding the saved ISO from the current checkout.' if getattr(_args, 'rebuild', False)
                  else 'Saved ISO source is outdated or unknown; rebuilding automatically.')
            source = build_base(directory)
            config = yaml.safe_load(protected(config_path).read_text())
            auth_record(key, reusable=True)
            replacement = build_image(config, 'iso', installer / ('image-build-' + str(time.time_ns())), source)
            record_iso_source(replacement, current_source)
            auth_record(key, reusable=True)
            previous = installer / ('image-previous-' + str(time.time_ns()))
            image.parent.rename(previous)
            replacement.parent.rename(image.parent)
            print(f'Previous ISO and build logs saved at {previous}')
        else:
            print('Reusing the saved ISO; its commit and source files match the current checkout.')
    else:
        if installer.exists() and set(installer.iterdir()) != {key}:
            raise ValueError(f'{installer} contains an incomplete build; inspect its protected logs and revoke '
                             'the batch key before archiving it and starting a new batch')
        wifi = operator_wifi(directory)
        private_command(['sudo', '--', 'docker', 'info', '--format', '{{.OSType}}'], directory)
        source = build_base(directory)
        if not installer.exists():
            output_directory(installer)
            mint(settings['tailscale_api_token'].strip(), settings['tailnet'], 'worker', key, reusable=True)
        record = auth_record(key, reusable=True)
        config = machine_config(record['key'], (directory / 'ssh_key.pub').read_text(), wifi=wifi)
        private_write(config_path, cloud_yaml(config))
        image = build_image(config, 'iso', installer / 'image', source)
        record_iso_source(image, current_source)
        auth_record(key, reusable=True)
    print(f'Private shared ISO ready: {image}')
    print(f'ISO source: commit {current_source["commit"][:12]}, build inputs {current_source["sources_sha256"][:12]}.')
    expires = datetime.fromisoformat(record['expires'].replace('Z', '+00:00')).astimezone()
    print(f'New Tailscale registrations expire on {expires:%A, %B %-d, %Y at %-I:%M:%S %p %Z}.')
    if flash_iso(image):
        print('Boot the laptop from this USB. Wi-Fi connects automatically.')
        print('Then run ./guaranine worker on this operator computer.')
    else:
        print('USB flashing was not completed. Run ./guaranine iso again to write the saved ISO.')


def prepare_controlplane(_args):
    directory = operator_directory()
    settings = operator_settings()
    node = directory / 'nodes' / role_config('controlplane')['hostname']
    key, config_path = node / 'tailscale-key.json', node / 'config.yaml'
    image = node / 'image/guaranine-kairos.vhd'
    if image.exists():
        auth_record(key)
        protected(image)
    else:
        private_command(['sudo', '--', 'docker', 'info', '--format', '{{.OSType}}'], directory)
        source = build_base(directory)
        if not node.exists():
            output_directory(node)
            mint(settings['tailscale_api_token'].strip(), settings['tailnet'], 'controlplane', key)
        record = auth_record(key)
        if (node / 'image').exists():
            raise ValueError('Incomplete control-plane image; inspect protected logs before retrying')
        config = machine_config(record['key'], (directory / 'ssh_key.pub').read_text(),
            role='controlplane',
            token=protected(directory / 'server-token').read_text(),
            agent_token=protected(directory / 'agent-token').read_text())
        if not config_path.exists():
            private_write(config_path, cloud_yaml(config))
        else:
            config = yaml.safe_load(protected(config_path).read_text())
        image = build_image(config, 'vhd', node / 'image', source)
        auth_record(key)
    print(f'Private control-plane VHD ready: {image}')
    print('No Azure resources were changed. The K3s data volume must be prepared separately.')


def ssh_command(address, hosts):
    address = tailnet_ip(address)
    key = protected(operator_directory() / 'ssh_key')
    return ['ssh', '-i', str(key), '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={protected(hosts)}',
            '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'HostKeyAlgorithms=ssh-ed25519',
            '-o', 'ConnectTimeout=20', '--', 'root@' + address]


@contextmanager
def verified_connection(address, directory):
    """Untrusted keyscan becomes trusted only after a physical fingerprint comparison."""
    address = tailnet_ip(address)
    deadline = time.monotonic() + 120
    with progress(f'Waiting for SSH on {address}…'):
        while True:
            try:
                scan = subprocess.run(['ssh-keyscan', '-T', '5', '-t', 'ed25519', address],
                                      capture_output=True, text=True, timeout=10)
                lines = [line for line in scan.stdout.splitlines() if line and not line.startswith('#')]
                if scan.returncode == 0 and lines:
                    break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= deadline:
                raise ValueError('SSH did not become available. Leave the laptop booted from the USB and try again.')
            time.sleep(2)
    if len(lines) != 1:
        raise ValueError('Expected exactly one SSH Ed25519 host key')
    fields = lines[0].split()
    if len(fields) != 3 or fields[:2] != [address, 'ssh-ed25519']:
        raise ValueError('Unexpected SSH keyscan response')
    fingerprint = private_command(['ssh-keygen', '-lf', '-', '-E', 'sha256'], directory,
                                  data=lines[0] + '\n', timeout=30).split()[1]
    print(f'\nConnecting to {address}. Its SSH fingerprint is:')
    print(f'\n  {fingerprint}\n')
    print('Compare this with the SSH fingerprint on the laptop screen or protected VM console.')
    if input('Do they match? Type yes to continue (Enter to cancel): ').strip().lower() not in ('y', 'yes'):
        raise ValueError('SSH identity not confirmed; no remote command was sent')
    with tempfile.TemporaryDirectory(prefix='ssh-', dir=directory) as scratch:
        hosts = Path(scratch) / 'known_hosts'
        private_write(hosts, lines[0] + '\n')
        yield ssh_command(address, hosts), hosts


def read_disks(connection, directory):
    raw = private_command([*connection,
        'lsblk --json --bytes --paths --tree --output PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RO,MOUNTPOINTS'], directory)
    disks = []
    for disk in json.loads(raw)['blockdevices']:
        family = [disk]
        for member in family:
            family.extend(member.get('children', []))
        if (disk['type'] == 'disk' and re.fullmatch(DISK_PATH, disk['path'])
                and not disk.get('ro') and disk['size'] >= 16 * 2**30
                and not any(mount for member in family for mount in member.get('mountpoints', []) if mount)):
            disks.append({field: disk.get(field) for field in ('path', 'size', 'model', 'serial', 'tran')})
    return sorted(disks, key=lambda disk: disk['path'])


def initialize_data(connection, directory):
    present = private_command([*connection,
        'test ! -e /dev/disk/by-label/GUARANINE_DATA || printf present'], directory)
    if present == 'present':
        return
    disks = read_disks(connection, directory)
    if not disks:
        raise ValueError('No unmounted K3s data disk found; attach the retained disk before bootstrap')
    for number, disk in enumerate(disks, 1):
        print(f'{number}. {disk["path"]} | {disk["size"] / 2**30:.1f} GiB | '
              f'{disk["model"] or "Unknown model"} | {disk["serial"] or "No serial"}')
    choice = input('NEW EMPTY retained data disk number (no default): ').strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(disks):
        raise ValueError('Data disk initialization cancelled')
    selected = disks[int(choice) - 1]
    disk = selected['path']
    signatures = json.loads(private_command([*connection, 'wipefs --no-act --json ' + shlex.quote(disk)], directory))
    if signatures.get('signatures'):
        raise ValueError('The disk contains existing signatures; restore or attach a NEW EMPTY disk instead')
    if input(f'Type INITIALIZE {disk} to format that empty disk for K3s: ').strip() != 'INITIALIZE ' + disk:
        raise ValueError('Data disk initialization cancelled')
    if selected not in read_disks(connection, directory):
        raise ValueError('The selected data disk changed; nothing was formatted')
    signatures = json.loads(private_command([*connection, 'wipefs --no-act --json ' + shlex.quote(disk)], directory))
    if signatures.get('signatures'):
        raise ValueError('The data disk acquired filesystem signatures; nothing was formatted')
    private_command([*connection, 'mkfs.ext4 -L GUARANINE_DATA -- ' + shlex.quote(disk)], directory)


def bootstrap(_args):
    directory = operator_directory()
    settings = operator_settings()
    name = role_config('controlplane')['hostname']
    check_name(name)
    if (directory / 'kubeconfig').exists() or (directory / 'controlplane-known-hosts').exists():
        raise ValueError('Control-plane operator outputs already exist; inspect them before repeating bootstrap')
    address = tailnet_ip(input('Control-plane Tailscale IPv4 address: ').strip())
    with verified_connection(address, directory) as (connection, hosts):
        initialize_data(connection, directory)
        private_command([*connection, 'systemctl start --no-block k3s.service'], directory)
        raw = private_command([*connection, 'timeout 180 sh -ec \'until k3s kubectl wait --for=condition=Ready '
            f'node/{name} --timeout=10s >/dev/null 2>&1; '
            'do sleep 2; done; cat /etc/rancher/k3s/k3s.yaml\''], directory)
        kubeconfig = yaml.safe_load(raw)
        cluster_endpoint = 'https://' + address + ':6443'
        for cluster in kubeconfig['clusters']:
            cluster['cluster']['server'] = cluster_endpoint
        private_write(directory / 'kubeconfig', yaml.safe_dump(kubeconfig))
        private_write(directory / 'controlplane-known-hosts', hosts.read_text())
    settings['endpoint'] = cluster_endpoint
    temporary = directory / 'operator.yaml.new'
    private_write(temporary, yaml.safe_dump(settings, sort_keys=False))
    temporary.replace(protected(directory / 'operator.yaml'))
    print(f'K3s is ready. Use: export KUBECONFIG={shlex.quote(str(directory / "kubeconfig"))}')


def choose_laptop(directory):
    print('Boot the laptop from the Guaranine USB and leave it powered on.', flush=True)
    print('Waiting for a laptop to connect over Wi-Fi and Tailscale (Ctrl+C to cancel)…', flush=True)
    while True:
        status = json.loads(private_command(['tailscale', 'status', '--json'], directory, timeout=30))
        if status.get('BackendState') != 'Running':
            raise ValueError('Connect this operator computer to Tailscale, then run ./guaranine worker again')
        laptops = []
        for peer in (status.get('Peer') or {}).values():
            name = peer.get('HostName') or ''
            if (not peer.get('Online') or not name.startswith('guaranine-live-')
                    or 'tag:guaranine-worker' not in (peer.get('Tags') or [])):
                continue
            address = next((ip for ip in (peer.get('TailscaleIPs') or []) if ':' not in ip), None)
            if address:
                laptops.append((name, tailnet_ip(address)))
        if laptops:
            break
        time.sleep(2)
    laptops.sort()
    if len(laptops) == 1:
        name, address = laptops[0]
        print(f'Found one laptop: {name} | {address}')
        return address
    print('\nLaptops booted from the Guaranine USB:')
    for number, (name, address) in enumerate(laptops, 1):
        print(f'{number}. {name} | {address}')
    choice = input('Laptop number to install (Enter to cancel): ').strip()
    if not choice.isdecimal() or not 1 <= int(choice) <= len(laptops):
        raise ValueError('Laptop selection cancelled; no installation was started')
    return laptops[int(choice) - 1][1]


def wait_for_worker(name, server, node):
    check_name(name)
    wait = ('until k3s kubectl wait --for=condition=Ready node/' + name + ' --timeout=10s >/dev/null 2>&1; '
            'do sleep 2; done; k3s kubectl get node ' + name + ' -o json')
    try:
        with progress(f'Waiting for {name} to become Ready. Keep the USB removed and the laptop powered on…'):
            ready = json.loads(private_command([*server, 'timeout 600 sh -ec ' + shlex.quote(wait)], node, timeout=630))
    except (ValueError, subprocess.TimeoutExpired) as error:
        raise ValueError(f'{name} has not been confirmed Ready. Check that the USB is removed and the laptop is powered on. '
                         f'Resume this check with ./guaranine worker --wait {name}. Diagnostics: {error}') from error
    if not any(condition.get('type') == 'Ready' and condition.get('status') == 'True'
               for condition in ready['status']['conditions']):
        raise ValueError(f'{name} is not Ready yet; check the laptop screen')
    addresses = [item['address'] for item in ready['status']['addresses'] if item['type'] == 'InternalIP']
    if len(addresses) != 1:
        raise ValueError(f'{name} must have exactly one internal Tailscale IPv4 address')
    print(f'\n{name} is Ready at {tailnet_ip(addresses[0])}. Connected to the controller.')


def install_worker(_args):
    directory = operator_directory()
    settings = operator_settings()
    cluster_endpoint = endpoint(settings.get('endpoint'))
    server = ssh_command(urllib.parse.urlsplit(cluster_endpoint).hostname, directory / 'controlplane-known-hosts')
    if getattr(_args, 'wait', None):
        name = _args.wait
        check_name(name)
        node = directory / 'nodes' / name
        protected(node / 'config.yaml')
        wait_for_worker(name, server, node)
        return
    key = directory / 'installer/tailscale-key.json'
    auth_record(key, reusable=True)
    address = choose_laptop(directory)
    with verified_connection(address, directory) as (connection, _hosts):
        live = private_command([*connection, 'test -f /run/cos/live_mode && '
            'test -d /run/rootfsbase && printf live'], directory)
        if live != 'live':
            raise ValueError('The laptop must be booted from the shared Kairos ISO')
        model = private_command([*connection, 'cat /sys/class/dmi/id/product_name'], directory).strip()
        print(f'\nLaptop model: {model!r}')
        existing = json.loads(private_command([*server, 'k3s kubectl get nodes -o json'], directory))
        used_names = {item['metadata']['name'] for item in existing['items']}
        if (directory / 'nodes').exists():
            used_names.update(path.name for path in (directory / 'nodes').iterdir())
        stem = re.sub(r'[^a-z0-9]+', '-', model.lower()).strip('-')[:48].rstrip('-') or 'laptop'
        if not stem[0].isalpha() or len(stem) < 3:
            stem = 'worker-' + stem
        suggested, suffix = stem, 2
        while suggested in used_names:
            suggested = f'{stem}-{suffix}'
            suffix += 1
        while True:
            name = input(f'Laptop name (Enter for {suggested}): ').strip() or suggested
            try:
                check_name(name)
                if name in used_names:
                    raise ValueError('That name is already used. Choose another name.')
                break
            except ValueError as error:
                print(error)
        node = directory / 'nodes' / name
        disks = read_disks(connection, directory)
        if not disks:
            raise ValueError('No unmounted, writable installation disks of at least 16 GiB were detected')
        for number, disk in enumerate(disks, 1):
            print(f'{number}. {disk["path"]} | {disk["size"] / 2**30:.1f} GiB | '
                  f'{disk["model"] or "Unknown model"} | {disk["serial"] or "No serial"} | {disk["tran"] or "Unknown transport"}')
        choice = input('Installation disk number (no default): ').strip()
        if not choice.isdecimal() or not 1 <= int(choice) <= len(disks):
            raise ValueError('Installation cancelled; select one of the displayed disks')
        selected = disks[int(choice) - 1]
        disk = selected['path']
        confirmation = 'ERASE'
        print(f'Install {name} on {address}, destroying ALL DATA on {disk}.')
        print(f'Disk: {selected["model"] or "Unknown model"} | {selected["size"] / 2**30:.1f} GiB | '
              f'{selected["serial"] or "No serial"} | {disk}')
        if input('Type ERASE to erase this disk and install (Enter to cancel): ').strip().upper() != confirmation:
            raise ValueError('Installation cancelled; no configuration was applied')
        if selected not in read_disks(connection, directory):
            raise ValueError('The selected disk changed; no installation was started')
        record = auth_record(key, reusable=True)
        agent_token = private_command([*server, 'cat /var/lib/rancher/k3s/server/agent-token'], directory).strip()
        if not re.fullmatch(r'K10[0-9a-f]{64}::[^\s]+', agent_token):
            raise ValueError('Require the secure CA-pinned K3s agent token from the control plane')
        boot = yaml.safe_load(protected(directory / 'installer/boot.yaml').read_text())
        wifi = next(file['content'] for stage in boot['stages']['initramfs']
                    for file in stage.get('files', []) if file['path'] == '/etc/guaranine/wifi.conf')
        config = machine_config(record['key'], (directory / 'ssh_key.pub').read_text(), name=name,
                                role='worker', cluster_endpoint=cluster_endpoint, token=agent_token, wifi=wifi)
        output_directory(node)
        private_write(node / 'config.yaml', cloud_yaml(config))
        auth_record(key, reusable=True)
        # No custom node API. Native Kairos installs from the read-only live root.
        command = 'kairos-agent --strict-validation manual-install --device ' + shlex.quote(disk) + ' /dev/stdin'
        try:
            with progress(f'Installing {name} on {disk}…'):
                private_command([*connection, command], node, data=cloud_yaml(config), timeout=1800)
        except (ValueError, subprocess.TimeoutExpired) as error:
            print(f'Installation could not be confirmed: {error}')
            print('Check the laptop screen. An installation that completed may close SSH while powering off.')
    print('\nWhen the laptop powers off: remove the USB, then turn the laptop on again.', flush=True)
    print('This command will confirm when the laptop joins the controller.', flush=True)
    wait_for_worker(name, server, node)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='guaranine', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create private Kairos operator files once').set_defaults(
        action=lambda _args: initialize(operator_directory()))
    iso = commands.add_parser('iso', help='Build/update a private Wi-Fi-capable ISO and offer USB flashing')
    iso.add_argument('--rebuild', action='store_true', help='Force a rebuild even when the saved ISO source matches')
    iso.set_defaults(action=prepare_iso)
    commands.add_parser('controlplane', help='Build/reuse the private Azure VHD').set_defaults(action=prepare_controlplane)
    commands.add_parser('bootstrap', help='Connect to the prepared cloud server and save its kubeconfig').set_defaults(action=bootstrap)
    worker = commands.add_parser('worker', help='Install a laptop and wait for it to become Ready')
    worker.add_argument('--wait', metavar='NAME', help='Resume only the Ready check for an already prepared worker')
    worker.set_defaults(action=install_worker)
    os.umask(0o077)
    try:
        args = parser.parse_args(argv)
        args.action(args)
    except yaml.YAMLError:
        parser.exit(1, 'Invalid private YAML; check the operator/configuration file locally.\n')
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, 'Cancelled. If installation was already sent, check the laptop before retrying.\n')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()

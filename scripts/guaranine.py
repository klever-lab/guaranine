#!/usr/bin/env python3
"""Build Kairos images, install laptops and roll out host configuration over Tailscale."""
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

import questionary
import yaml

ROOT = Path(__file__).resolve().parents[1]
PINS = json.loads((ROOT / 'kairos/versions.json').read_text())
TAILNET = ipaddress.ip_network('100.64.0.0/10')
DISK_PATH = r'/dev/(sd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)'


class PrivateCommandError(ValueError):
    def __init__(self, returncode, log):
        super().__init__(f'Command failed (exit {returncode}). Private log: {log}')
        self.returncode = returncode


class ExpiredAuthKey(ValueError):
    pass


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
        raise PrivateCommandError(result.returncode, path)
    return result.stdout


def auth_record(path, now=None, *, reusable=False):
    record = json.loads(protected(path).read_text())
    created = datetime.fromisoformat(record['created'].replace('Z', '+00:00'))
    expires = datetime.fromisoformat(record['expires'].replace('Z', '+00:00'))
    now = now or datetime.now(timezone.utc)
    capability = record['capabilities']['devices']['create']
    # Tolerate small API/operator clock differences without extending expiry.
    if not (created.tzinfo and expires.tzinfo and 0 < (expires-created).total_seconds() <= 86400
            and created <= now + timedelta(seconds=5) and capability['reusable'] is reusable
            and capability['ephemeral'] is False and capability['preauthorized'] is True
            and capability.get('tags') == ['tag:guaranine-' + ('worker' if reusable else 'controlplane')]
            and isinstance(record['key'], str) and record['key'].startswith('tskey-auth-')):
        use = 'reusable' if reusable else 'single-use'
        raise ValueError(f'Require an unexpired, preauthorized, non-ephemeral, {use} Tailscale key with at most 24 hours lifetime')
    if now >= expires:
        raise ExpiredAuthKey('Tailscale enrollment key expired')
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
            print('No suitable USB drives found.')
            return False
        choices = [questionary.Choice('Skip flashing', value=False)]
        for disk in disks:
            labels = ', '.join(member['label'] for member in [disk, *disk.get('children', [])] if member.get('label'))
            choices.append(questionary.Choice(
                f'{disk["path"]} | {disk.get("model") or "Unknown model"} | '
                f'{disk["size"] / 2**30:.1f} GiB | {disk.get("serial") or "No serial"} | {labels or "No label"}',
                value=disk))
        selected = questionary.select('Flash the ISO to a USB drive', choices=choices).unsafe_ask()
        if not selected:
            return False
        path = selected['path']
        confirmation = 'ERASE'
        print(f'ALL DATA on {path} will be erased. Image: {image.name}')
        answer = input('Type ERASE to write this USB (Enter to cancel): ').strip().upper()
        if answer != confirmation:
            if answer:
                print(f'Confirmation must be {confirmation}.')
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
        print(f'Writing {image.stat().st_size / 2**20:.1f} MiB to {path}…', flush=True)
        print('Do not remove the USB.', flush=True)
        log = image.parent / ('flash-' + str(time.time_ns()) + '.log')
        private_write(log, f'Image: {image}\nUSB: {path}\n')
        print(f'Flash log: {log}', flush=True)
        try:
            with subprocess.Popen(['tee', '--append', '--', str(log)], stdin=subprocess.PIPE,
                                  stdout=sys.stderr) as logger:
                with logger.stdin:
                    subprocess.run(['sudo', '--', 'dd', f'if={image}', f'of={path}', 'bs=4M', 'oflag=dsync',
                                    'status=progress', 'conv=fsync,nocreat'],
                                   stdout=logger.stdin, stderr=subprocess.STDOUT, check=True, timeout=900)
            if logger.returncode:
                raise subprocess.CalledProcessError(logger.returncode, ['tee'])
        except (OSError, subprocess.SubprocessError) as error:
            with log.open('a') as saved:
                saved.write(f'\n{type(error).__name__}: {error}\n')
            raise ValueError(f'USB write failed; the USB may be incomplete. ISO saved: {image}. '
                             f'Private log: {log}') from error
        print(f'\nFlashed {image.name} to {path}. You can remove the USB.')
        return True
    except (EOFError, KeyboardInterrupt):
        print(f'\nFlashing cancelled. ISO saved: {image}')
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
    print(f'Created operator files. Edit {directory / "operator.yaml"}.')


def operator_settings():
    directory = operator_directory()
    settings = yaml.safe_load(protected(directory / 'operator.yaml').read_text())
    if not isinstance(settings, dict) or not all(
            isinstance(settings.get(field), str) and settings[field].strip()
            and not settings[field].startswith('REPLACE_') for field in ('tailnet', 'tailscale_api_token')):
        raise ValueError('Set tailnet and tailscale_api_token in operator.yaml first')
    token = settings['tailscale_api_token'].strip()
    if not token.isascii() or any(character.isspace() for character in token):
        raise ValueError('Use one ASCII Tailscale API token without whitespace')
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
                             + (ROOT / 'kairos/config.yaml').read_bytes()
                             + (ROOT / 'kairos/lid.yaml').read_bytes()).hexdigest()[:16]
    image = 'guaranine-kairos:' + identity
    args = {'K3S_VERSION': PINS['k3s'], 'K3S_SHA256': PINS['k3s_sha256']}
    command = ['sudo', '--', 'docker', 'build', '--platform', 'linux/amd64', '--tag', image]
    for key, value in args.items():
        command += ['--build-arg', f'{key}={value}']
    with progress('Building Kairos base…'):
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
    with (progress(f'Building Kairos {image_format.upper()}…'),
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
            raise ValueError(f'Kairos build failed (exit {result.returncode}). Private log: {log}')
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
    renew = False
    if image.exists():
        protected(image)
        try:
            record = auth_record(key, reusable=True)
        except ExpiredAuthKey:
            config = yaml.safe_load(protected(config_path).read_text())
            wifi = next((file['content'] for step in config['stages']['initramfs']
                         for file in step.get('files', []) if file['path'] == '/etc/guaranine/wifi.conf'), None)
            if not wifi:
                raise ValueError('Saved ISO Wi-Fi settings are missing; inspect installer/boot.yaml before renewing')
            renew = True
            print('ISO enrollment key expired; creating a new batch with the saved Wi-Fi settings…')
    if image.exists() and not renew:
        saved_source = None
        metadata = image.parent / 'source.json'
        if metadata.exists():
            try:
                saved_source = json.loads(protected(metadata).read_text())
            except (json.JSONDecodeError, UnicodeError):
                pass
        if getattr(_args, 'rebuild', False) or saved_source != current_source:
            print('Rebuilding ISO…' if getattr(_args, 'rebuild', False)
                  else 'ISO source changed or is unknown; rebuilding…')
            source = build_base(directory)
            config = yaml.safe_load(protected(config_path).read_text())
            auth_record(key, reusable=True)
            replacement = build_image(config, 'iso', installer / ('image-build-' + str(time.time_ns())), source)
            record_iso_source(replacement, current_source)
            auth_record(key, reusable=True)
            previous = installer / ('image-previous-' + str(time.time_ns()))
            image.parent.rename(previous)
            replacement.parent.rename(image.parent)
            print(f'Previous ISO and logs: {previous}')
        else:
            print('Reusing saved ISO (source unchanged).')
    else:
        if not renew and installer.exists() and set(installer.iterdir()) != {key}:
            raise ValueError(f'{installer} contains an incomplete build; inspect its protected logs and revoke '
                             'the batch key before archiving it and starting a new batch')
        if not renew:
            wifi = operator_wifi(directory)
        private_command(['sudo', '--', 'docker', 'info', '--format', '{{.OSType}}'], directory)
        source = build_base(directory)
        if renew:
            previous = directory / ('installer-expired-' + str(time.time_ns()))
            installer.rename(previous)
            print(f'Expired ISO batch and logs: {previous}')
        if not installer.exists():
            output_directory(installer)
            mint(settings['tailscale_api_token'].strip(), settings['tailnet'], 'worker', key, reusable=True)
        record = auth_record(key, reusable=True)
        config = machine_config(record['key'], (directory / 'ssh_key.pub').read_text(), wifi=wifi)
        private_write(config_path, cloud_yaml(config))
        image = build_image(config, 'iso', installer / 'image', source)
        record_iso_source(image, current_source)
        auth_record(key, reusable=True)
    print(f'ISO ready: {image}')
    print(f'ISO source: commit {current_source["commit"][:12]}, inputs {current_source["sources_sha256"][:12]}.')
    expires = datetime.fromisoformat(record['expires'].replace('Z', '+00:00')).astimezone()
    print(f'Enroll laptops before {expires:%Y-%m-%d %H:%M:%S %Z}.')
    if flash_iso(image):
        print('Boot the laptop from this USB. Wi-Fi connects automatically.')
        print('On this computer, run: ./guaranine worker')
    else:
        print('USB not flashed. Run ./guaranine iso to try again.')


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
    print(f'Controller VHD ready: {image}')
    print('Azure is unchanged. Prepare the K3s data volume separately.')


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
                raise ValueError('SSH unavailable. Keep the laptop booted from the USB and try again.')
            time.sleep(2)
    if len(lines) != 1:
        raise ValueError('Expected exactly one SSH Ed25519 host key')
    fields = lines[0].split()
    if len(fields) != 3 or fields[:2] != [address, 'ssh-ed25519']:
        raise ValueError('Unexpected SSH keyscan response')
    fingerprint = private_command(['ssh-keygen', '-lf', '-', '-E', 'sha256'], directory,
                                  data=lines[0] + '\n', timeout=30).split()[1]
    print(f'\nSSH fingerprint for {address}:')
    print(f'\n  {fingerprint}\n')
    print('Compare with the fingerprint on the laptop screen or protected VM console.')
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


def check_worker_boot(connection, directory):
    status = private_command([*connection, '''if test ! -d /sys/firmware/efi; then
  printf bios
elif ! command -v efibootmgr >/dev/null || ! efibootmgr --version >/dev/null 2>&1; then
  printf outdated
elif ! test -w /sys/firmware/efi/efivars ||
     ! findmnt -nro OPTIONS --mountpoint /sys/firmware/efi/efivars | grep -Eq '(^|,)rw(,|$)' ||
     ! efibootmgr >/dev/null 2>&1; then
  printf unavailable
else
  printf uefi
fi'''], directory)
    if status == 'outdated':
        raise ValueError('USB needs updating. Run ./guaranine iso, reflash it and boot the laptop again. Nothing was erased.')
    if status not in ('bios', 'uefi'):
        raise ValueError('UEFI boot settings are unavailable or read-only. Nothing was erased; check the laptop screen.')


def check_install_labels(connection, directory, selected):
    """Kairos resolves partition/filesystem labels globally, even when given a disk."""
    raw = private_command([*connection, 'lsblk --json --paths --tree --output PATH,LABEL,PARTLABEL'], directory)
    labels = {'COS_GRUB', 'COS_OEM', 'COS_STATE', 'COS_RECOVERY', 'COS_PERSISTENT'}
    names = {'efi', 'oem', 'state', 'recovery', 'persistent'}
    pending = list(json.loads(raw)['blockdevices'])
    while pending:
        disk = pending.pop()
        if disk['path'] == selected:
            continue
        if disk.get('label') in labels or disk.get('partlabel') in names:
            raise ValueError(f'{disk["path"]} has a conflicting Kairos disk label. Disconnect that drive before installing. Nothing was erased.')
        pending.extend(disk.get('children', []))


def initialize_data(connection, directory):
    present = private_command([*connection,
        'test ! -e /dev/disk/by-label/GUARANINE_DATA || printf present'], directory)
    if present == 'present':
        return
    disks = read_disks(connection, directory)
    if not disks:
        raise ValueError('No unmounted K3s data disk found; attach the retained disk before bootstrap')
    selected = questionary.select('NEW EMPTY retained data disk', choices=[
        questionary.Choice('Cancel', value=False),
        *[questionary.Choice(f'{disk["path"]} | {disk["size"] / 2**30:.1f} GiB | '
                            f'{disk["model"] or "Unknown model"} | {disk["serial"] or "No serial"}', value=disk)
          for disk in disks],
    ]).unsafe_ask()
    if not selected:
        raise ValueError('Data disk initialization cancelled')
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
        raise ValueError('kubeconfig or controlplane-known-hosts already exists; inspect before rerunning bootstrap')
    address = tailnet_ip(input('Controller Tailscale IPv4 address: ').strip())
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
    print(f'K3s ready. Run: export KUBECONFIG={shlex.quote(str(directory / "kubeconfig"))}')


def choose_laptop(directory):
    print('Boot the laptop from the Guaranine USB and keep it on.', flush=True)
    print('Waiting for a laptop on Tailscale (Ctrl+C to cancel)…', flush=True)
    while True:
        status = json.loads(private_command(['tailscale', 'status', '--json'], directory, timeout=30))
        if status.get('BackendState') != 'Running':
            raise ValueError('Connect this computer to Tailscale, then rerun ./guaranine worker')
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
        print(f'Found laptop: {name} | {address}')
        return address
    address = questionary.select('Laptop to install', choices=[
        questionary.Choice('Cancel', value=False),
        *[questionary.Choice(f'{name} | {address}', value=address) for name, address in laptops],
    ]).unsafe_ask()
    if not address:
        raise ValueError('Laptop selection cancelled; no installation was started')
    return address


def wait_for_worker(name, server, node):
    check_name(name)
    wait = ('until k3s kubectl wait --for=condition=Ready node/' + name + ' --timeout=10s >/dev/null 2>&1; '
            'do sleep 2; done; k3s kubectl get node ' + name + ' -o json')
    try:
        with progress(f'Waiting for {name} to become Ready…'):
            ready = json.loads(private_command([*server, 'timeout 600 sh -ec ' + shlex.quote(wait)], node, timeout=630))
    except (ValueError, subprocess.TimeoutExpired) as error:
        raise ValueError(f'Could not confirm {name} is Ready. Check the laptop is on with the USB removed. '
                         f'Resume: ./guaranine worker --wait {name}. Details: {error}') from error
    if not any(condition.get('type') == 'Ready' and condition.get('status') == 'True'
               for condition in ready['status']['conditions']):
        raise ValueError(f'{name} is not Ready yet; check the laptop screen')
    addresses = [item['address'] for item in ready['status']['addresses'] if item['type'] == 'InternalIP']
    if len(addresses) != 1:
        raise ValueError(f'{name} must have exactly one internal Tailscale IPv4 address')
    print(f'\n{name} is Ready at {tailnet_ip(addresses[0])}.')


def forget_worker(name, server, directory):
    check_name(name)
    if name == role_config('controlplane')['hostname']:
        raise ValueError('Cannot forget the controller')
    node = directory / 'nodes' / name
    try:
        info = node.lstat()
    except FileNotFoundError:
        raise ValueError(f'No saved installation for {name}') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Saved worker directory must be operator-owned with mode 0700')
    existing = json.loads(private_command([*server, 'k3s kubectl get nodes -o json'], directory))
    if any(item['metadata']['name'] == name for item in existing['items']):
        raise ValueError(f'{name} is registered in Kubernetes; retire that worker before forgetting its files')
    backups = directory / 'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    info = backups.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Backup directory must be operator-owned with mode 0700')
    archive = backups / (name + '-install-' + str(time.time_ns()))
    node.rename(archive)
    print(f'Archived {name}: {archive}')
    print(f'{name} is available for installation. Run: ./guaranine worker')


def manage_workers(args):
    if getattr(args, 'wait', None):
        install_worker(args)
        return
    action = questionary.select('Worker action', choices=[
        questionary.Choice('Cancel', value=False),
        questionary.Choice('Add worker', value='add'),
        questionary.Choice('Forget worker', value='forget'),
    ]).unsafe_ask()
    if action == 'add':
        install_worker(args)
        return
    if action != 'forget':
        raise ValueError('Worker action cancelled; select Add worker or Forget worker')
    directory = operator_directory()
    nodes = directory / 'nodes'
    controller = role_config('controlplane')['hostname']
    names = []
    if nodes.exists():
        names = sorted(path.name for path in nodes.iterdir()
                       if path.is_dir() and not path.is_symlink() and path.name != controller)
    if not names:
        print('No saved workers to forget.')
        return
    name = questionary.select('Worker to forget', choices=[
        questionary.Choice('Cancel', value=False),
        *[questionary.Choice(name, value=name) for name in names],
    ]).unsafe_ask()
    if not name:
        raise ValueError('Worker selection cancelled; no files were archived')
    settings = operator_settings()
    address = urllib.parse.urlsplit(endpoint(settings.get('endpoint'))).hostname
    server = ssh_command(address, directory / 'controlplane-known-hosts')
    forget_worker(name, server, directory)


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
            raise ValueError('Boot the laptop from the Guaranine USB')
        check_worker_boot(connection, directory)
        model = private_command([*connection, 'cat /sys/class/dmi/id/product_name'], directory).strip()
        print(f'\nLaptop model: {model!r}')
        existing = json.loads(private_command([*server, 'k3s kubectl get nodes -o json'], directory))
        registered_names = {item['metadata']['name'] for item in existing['items']}
        used_names = set(registered_names)
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
                if name in registered_names:
                    raise ValueError('Name already registered in Kubernetes. Choose another.')
                if (directory / 'nodes' / name).exists():
                    raise ValueError('Name has saved installation files. To clear a failed attempt, run '
                                     './guaranine worker in another terminal, choose Forget worker, '
                                     f'select {name}, then enter the name again here.')
                break
            except ValueError as error:
                print(error)
        node = directory / 'nodes' / name
        disks = read_disks(connection, directory)
        if not disks:
            raise ValueError('No unmounted, writable disk of at least 16 GiB found')
        selected = questionary.select('Installation disk', choices=[
            questionary.Choice('Cancel', value=False),
            *[questionary.Choice(f'{disk["path"]} | {disk["size"] / 2**30:.1f} GiB | '
                                f'{disk["model"] or "Unknown model"} | {disk["serial"] or "No serial"} | '
                                f'{disk["tran"] or "Unknown transport"}', value=disk) for disk in disks],
        ]).unsafe_ask()
        if not selected:
            raise ValueError('Installation cancelled; no disk was selected')
        disk = selected['path']
        confirmation = 'ERASE'
        print(f'Install {name} on {address}, destroying ALL DATA on {disk}.')
        print(f'Disk: {selected["model"] or "Unknown model"} | {selected["size"] / 2**30:.1f} GiB | '
              f'{selected["serial"] or "No serial"} | {disk}')
        if input('Type ERASE to erase this disk and install (Enter to cancel): ').strip().upper() != confirmation:
            raise ValueError('Installation cancelled; no configuration was applied')
        if selected not in read_disks(connection, directory):
            raise ValueError('The selected disk changed; no installation was started')
        check_install_labels(connection, directory, disk)
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
        except (PrivateCommandError, subprocess.TimeoutExpired) as error:
            if isinstance(error, PrivateCommandError) and error.returncode != 255:
                raise ValueError(f'Installation failed. Keep the USB connected and check the laptop. {error}') from error
            print(f'Could not confirm installation: {error}')
            print('Check the laptop screen. SSH may close when installation finishes and the laptop powers off.')
    print('\nAfter the laptop powers off, remove the USB and turn it on again.\n', flush=True)
    wait_for_worker(name, server, node)


def rollout_script(contents, destination, host_root='/host'):
    """Use unique activation stages: Kairos v4.4.0 re-adds default paths even with --override."""
    prefix = 'guaranine-rollout-' + hashlib.sha256(contents.encode()).hexdigest()[:16] + '-'
    stages = yaml.safe_load(contents)['stages']
    activation = cloud_yaml({'stages': {prefix + stage: stages[stage]
                                       for stage in ('initramfs', 'boot') if stage in stages}})
    commands = '\n'.join('chroot "$rollout_host" kairos-agent run-stage --strict --override-cloud-init-paths '
                         '"${activation#"$rollout_host"}" ' + prefix + stage
                         for stage in ('initramfs', 'boot') if stage in stages)
    return f'''set -eu
# Use system D-Bus: the private systemd socket cannot resolve a peer in a child PID namespace.
export SYSTEMCTL_FORCE_BUS=1
rollout_host={shlex.quote(host_root)}
test ! -e "$rollout_host/run/cos/live_mode"
test -d "$rollout_host/oem"
candidate=$(mktemp "$rollout_host/oem/.guaranine-rollout-XXXXXX")
activation=$(mktemp "$rollout_host/run/guaranine-activation-XXXXXX")
trap 'rm -f "$candidate" "$activation"' EXIT
cat > "$candidate" <<'GUARANINE_CONFIG'
{contents.rstrip()}
GUARANINE_CONFIG
cat > "$activation" <<'GUARANINE_ACTIVATION'
{activation.rstrip()}
GUARANINE_ACTIVATION
chmod 0600 "$candidate" "$activation"
chroot "$rollout_host" kairos-agent --strict-validation validate "${{candidate#"$rollout_host"}}"
chroot "$rollout_host" kairos-agent --strict-validation validate "${{activation#"$rollout_host"}}"
mv -f "$candidate" "$rollout_host{destination}"
sync -f "$rollout_host/oem"
{commands}
printf 'Applied configuration successfully\\n'
'''


def rollout_config(args):
    """Submit one native Kairos configuration through the upstream NodeOp controller."""
    path = Path(args.config).resolve()
    if path.parent != ROOT / 'kairos' or not re.fullmatch(r'[a-z][a-z0-9-]{0,39}\.yaml', path.name):
        raise ValueError('Use a public rollout profile under kairos/ in this checkout')
    contents = path.read_text()
    profile = yaml.safe_load(contents)
    if (not isinstance(profile, dict) or set(profile) - {'name', 'stages'}
            or not isinstance(profile.get('stages'), dict) or not profile['stages']
            or set(profile['stages']) - {'initramfs', 'boot'}):
        raise ValueError('Rollout profiles may contain only name and initramfs/boot stages')
    selector = 'kairos.io/managed=true,!node-role.kubernetes.io/control-plane,!node-role.kubernetes.io/master'
    if args.node:
        check_name(args.node)
        selector += ',kubernetes.io/hostname=' + args.node
    def kubectl(*arguments, data=None):
        result = subprocess.run(['kubectl', '--request-timeout=30s', *arguments], input=data,
                                text=True, capture_output=True, timeout=45)
        if result.returncode:
            raise ValueError('kubectl failed: ' + result.stderr.strip())
        return result.stdout
    nodes = json.loads(kubectl('get', 'nodes', '-l', selector, '-o', 'json'))['items']
    if not nodes:
        raise ValueError('No managed workers match; install the Kairos Operator and check node labels')
    names = sorted(node['metadata']['name'] for node in nodes)
    if any(not any(condition['type'] == 'Ready' and condition['status'] == 'True'
                   for condition in node.get('status', {}).get('conditions', [])) for node in nodes):
        raise ValueError('All selected workers must be Ready; use --node NAME to target a ready worker')
    active = json.loads(kubectl('-n', 'kairos-operator', 'get', 'nodeops', '-o', 'json'))['items']
    if any(item.get('status', {}).get('phase') not in ('Completed', 'Failed') for item in active):
        raise ValueError('A NodeOp is already active; inspect kubectl -n kairos-operator get nodeops before retrying')
    destination = '/oem/91_guaranine-' + path.name
    operation = {
        'apiVersion': 'operator.kairos.io/v1alpha1', 'kind': 'NodeOp',
        'metadata': {'generateName': 'config-' + path.stem + '-', 'namespace': 'kairos-operator'},
        'spec': {'nodeSelector': {'matchExpressions': [
            {'key': 'kubernetes.io/hostname', 'operator': 'In', 'values': names}]},
            'command': ['sh', '-ec', rollout_script(contents, destination)], 'concurrency': 1, 'stopOnFailure': True,
            'backoffLimit': 0, 'cordon': False, 'rebootOnSuccess': False,
            'drainOptions': {'enabled': False},
            'resources': {'requests': {'cpu': '10m', 'memory': '64Mi'},
                          'limits': {'cpu': '500m', 'memory': '256Mi'}}}}
    print(f'Rolling out {path.name} to {", ".join(names)}', flush=True)
    created = json.loads(kubectl('create', '-f', '-', '-o', 'json', data=yaml.safe_dump(operation)))
    name = created['metadata']['name']
    print(f'NodeOp: {name}\nStatus: kubectl -n kairos-operator get nodeop {name} -o yaml', flush=True)
    deadline = time.monotonic() + 600
    previous = None
    while time.monotonic() < deadline:
        status = json.loads(kubectl('-n', 'kairos-operator', 'get', 'nodeop', name, '-o', 'json')).get('status', {})
        summary = ', '.join(f'{node}: {state.get("phase", "Pending")}'
                            for node, state in sorted(status.get('nodeStatuses', {}).items())) or 'Pending'
        if summary != previous:
            print(summary, flush=True)
            previous = summary
        if status.get('phase') == 'Failed':
            raise ValueError(f'Rollout {name} failed; inspect its status and Job logs before retrying')
        if (status.get('phase') == 'Completed'
                and set(status.get('nodeStatuses', {})) == set(names)
                and all(state.get('phase') == 'Completed' for state in status['nodeStatuses'].values())):
            print(f'Rollout {name} completed on all selected workers.')
            return
        time.sleep(2)
    raise ValueError(f'Timed out waiting for {name}; it continues in the cluster. Inspect status before retrying')


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog='guaranine', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create operator files (once)').set_defaults(
        action=lambda _args: initialize(operator_directory()))
    iso = commands.add_parser('iso', help='Build or update the laptop ISO; optionally flash a USB')
    iso.add_argument('--rebuild', action='store_true', help='Rebuild even if the source is unchanged')
    iso.set_defaults(action=prepare_iso)
    commands.add_parser('controlplane', help='Build or reuse the Azure VHD').set_defaults(action=prepare_controlplane)
    commands.add_parser('bootstrap', help='Start the cloud controller and save its kubeconfig').set_defaults(action=bootstrap)
    worker = commands.add_parser('worker', help='Add or forget a worker')
    worker.add_argument('--wait', metavar='NAME', help='Wait for Ready without reinstalling')
    worker.set_defaults(action=manage_workers)
    rollout = commands.add_parser('rollout', help='Apply a public Kairos configuration to managed workers')
    rollout.add_argument('config', metavar='CONFIG', help='Public profile under kairos/ (for example kairos/lid.yaml)')
    rollout.add_argument('--node', metavar='NAME', help='Target one worker instead of all workers')
    rollout.set_defaults(action=rollout_config)
    os.umask(0o077)
    try:
        args = parser.parse_args(argv or ['--help'])
        args.action(args)
    except yaml.YAMLError:
        parser.exit(1, 'Invalid YAML; check the local operator/configuration file.\n')
    except (EOFError, KeyboardInterrupt):
        if argv and argv[0] == 'rollout':
            parser.exit(1, 'Cancelled. A submitted NodeOp continues in the cluster; inspect its status before retrying.\n')
        parser.exit(1, 'Cancelled. If installation was already sent, check the laptop before retrying.\n')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()

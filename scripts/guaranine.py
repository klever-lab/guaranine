#!/usr/bin/env python3
"""Build private Kairos media and install K3s workers over Tailscale."""
import argparse
import base64
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
import tarfile
import tempfile
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
        return
    image = protected(image)
    try:
        disks = usb_disks(image)
        if not disks:
            print('No suitable USB drives found; the ISO is saved for later flashing.')
            return
        print('\nFlash the ISO to a USB drive:')
        print('0. Skip flashing (default)')
        for number, disk in enumerate(disks, 1):
            labels = ', '.join(member['label'] for member in [disk, *disk.get('children', [])] if member.get('label'))
            print(f'{number}. {disk["path"]} | {disk.get("model") or "Unknown model"} | '
                  f'{disk["size"] / 2**30:.1f} GiB | {disk.get("serial") or "No serial"} | {labels or "No label"}')
        choice = input('USB drive number (Enter to skip): ').strip()
        if choice in ('', '0'):
            return
        if not choice.isdecimal() or not 1 <= int(choice) <= len(disks):
            raise ValueError('Select a listed USB drive; nothing was flashed')
        selected = disks[int(choice) - 1]
        path = selected['path']
        confirmation = f'ERASE {path}'
        print(f'ALL DATA on {path} will be erased and replaced with {image.name}.')
        if input(f'Type {confirmation} to flash, or anything else to cancel: ').strip() != confirmation:
            print('Flashing cancelled.')
            return
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
        subprocess.run(['sudo', '--', 'dd', f'if={image}', f'of={path}', 'bs=4M', 'status=progress', 'conv=fsync,nocreat'],
                       check=True, timeout=900)
        print(f'Flashed {image.name} to {path}. You can now remove the USB drive.')
    except (EOFError, KeyboardInterrupt):
        print(f'\nFlashing cancelled; the ISO is saved at {image}.')


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
        'laptop_models': [],
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


def machine_config(key, public_key, *, name=None, role=None, cluster_endpoint=None, token=None, agent_token=None, wifi=None):
    """Native Kairos config; live media has neither an install target nor K3s authority."""
    files = [config_file('/etc/guaranine/tailscale-key', key + '\n')]
    config = {'hostname': name or 'guaranine-live-{{ trunc 8 .MachineID }}',
              'users': [{'name': 'root', 'groups': ['admin'], 'lock_passwd': True, 'ssh_authorized_keys': [public_key.strip()]}],
              'install': {'auto': False}}
    services = ['guaranine-tailscale.service']
    if wifi:
        files.append(config_file('/etc/guaranine/wifi.conf', wifi))
    if name:
        check_name(name)
    if role:
        if role not in ('worker', 'controlplane') or not name or not token:
            raise ValueError('A named worker or control plane needs its own K3s token')
        k3s = {'node-name': name, 'flannel-iface': 'tailscale0', 'token-file': '/etc/rancher/k3s/token',
               'resolv-conf': '/run/systemd/resolve/resolv.conf'}
        files.append(config_file('/etc/rancher/k3s/token', token.strip() + '\n'))
        mount, command = '', 'agent'
        if role == 'worker':
            k3s['server'] = endpoint(cluster_endpoint)
        else:
            if not agent_token or agent_token.strip() == token.strip():
                raise ValueError('The control plane needs a separate agent token')
            command = 'server'
            k3s.update({'agent-token': agent_token.strip(), 'flannel-backend': 'vxlan',
                        'secrets-encryption': True, 'write-kubeconfig-mode': '0600',
                        'disable': ['traefik', 'servicelb', 'local-storage', 'metrics-server', 'gateway-api-crd'],
                        'node-taint': ['node-role.kubernetes.io/control-plane=true:NoSchedule'],
                        'kube-apiserver-arg': ['admission-control-config-file=/etc/rancher/k3s/admission.yaml']})
            admission = {'apiVersion': 'apiserver.config.k8s.io/v1', 'kind': 'AdmissionConfiguration', 'plugins': [{
                'name': 'PodSecurity', 'configuration': {'apiVersion': 'pod-security.admission.config.k8s.io/v1',
                    'kind': 'PodSecurityConfiguration', 'defaults': {'enforce': 'restricted', 'enforce-version': 'latest',
                    'audit': 'restricted', 'audit-version': 'latest', 'warn': 'restricted', 'warn-version': 'latest'},
                    'exemptions': {'usernames': [], 'runtimeClasses': [], 'namespaces': ['kube-system']}}}]}
            files.append(config_file('/etc/rancher/k3s/admission.yaml', yaml.safe_dump(admission)))
            files.append(config_file('/etc/systemd/system/var-lib-rancher-k3s.mount', '[Unit]\n'
                'Description=Retained K3s data\nJobTimeoutSec=30\n[Mount]\n'
                'What=/dev/disk/by-label/GUARANINE_DATA\nWhere=/var/lib/rancher/k3s\nType=ext4\nOptions=defaults\n', 0o644))
            mount = 'BindsTo=var-lib-rancher-k3s.mount\nAfter=var-lib-rancher-k3s.mount\n'
            files.append(config_file('/etc/rancher/k3s/network-policy.yaml',
                                     (ROOT / 'kubernetes/network-policy.yaml').read_text()))
        files.append(config_file('/etc/rancher/k3s/config.yaml', yaml.safe_dump(k3s, sort_keys=False)))
        files.append(config_file('/etc/systemd/system/k3s.service', f'[Unit]\nDescription=K3s {command}\n'
            f'Requires=tailscaled.service guaranine-firewall.service\nWants=guaranine-tailscale.service\n'
            f'After=guaranine-tailscale.service guaranine-firewall.service\n{mount}[Service]\n'
            f'Environment=K3S_ROLE={command}\nType=notify\nDelegate=yes\nKillMode=process\n'
            'LimitNOFILE=1048576\nTasksMax=infinity\nRestart=always\nRestartSec=5\nTimeoutStartSec=0\n'
            'ExecStart=/bin/sh /etc/guaranine/k3s-start\n[Install]\nWantedBy=multi-user.target\n', 0o644))
        services.append('k3s.service')
    config['stages'] = {
        'initramfs': [{'name': 'Configure Guaranine identity and services', 'files': files,
                       'commands': ['systemctl enable ' + ' '.join(services)]}],
        'boot': [{'name': 'Start Guaranine services', 'commands': [
            'systemctl daemon-reload', 'systemctl start --no-block ' + ' '.join(services),
            'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub > /dev/console',
            'if test -c /dev/ttyS0; then ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub > /dev/ttyS0; fi']}],
    }
    return config


def cloud_yaml(config):
    return '#cloud-config\n' + yaml.safe_dump(config, sort_keys=False)


def build_base(directory):
    """Only public inputs enter the Docker build context and its cache."""
    identity = hashlib.sha256(json.dumps(PINS, sort_keys=True).encode()
                             + (ROOT / 'kairos/Dockerfile').read_bytes()
                             + (ROOT / 'kairos/config.yaml').read_bytes()).hexdigest()[:16]
    image = 'guaranine-kairos:' + identity
    args = {'K3S_VERSION': PINS['k3s'], 'K3S_SHA256': PINS['k3s_sha256'],
            'TAILSCALE_VERSION': PINS['tailscale'], 'TAILSCALE_SHA256': PINS['tailscale_sha256']}
    command = ['sudo', '--', 'docker', 'build', '--platform', 'linux/amd64', '--tag', image]
    for key, value in args.items():
        command += ['--build-arg', f'{key}={value}']
    print('Building the shared Kairos/Hadron base with K3s and Tailscale…', flush=True)
    private_command([*command, str(ROOT / 'kairos')], directory, timeout=1800)
    return image


def build_image(config, image_format, directory, source):
    """Run the pinned upstream builder offline. Private config only enters stdin."""
    directory = output_directory(directory)
    archive, log = directory / 'build-output.tar', directory / 'build.log'
    # Stage private inputs and disk files on protected operator storage.
    script = '''set -eu
umask 077
operator_uid=$3
operator_gid=$4
trap 'chown -R "$operator_uid:$operator_gid" /work' EXIT
mkdir -p /work/images
mkdir -p /work/tmp
export TMPDIR=/work/tmp
cat > /work/config.yaml
if [ "$1" = iso ]; then
  auroraboot build-iso --arch amd64 --cloud-config /work/config.yaml --output /work/images --override-name guaranine-kairos "$2" > /work/build.log 2>&1 || { cat /work/build.log >&2; exit 1; }
else
  auroraboot --cloud-config /work/config.yaml --set "container_image=$2" --set disable_http_server=true --set disable_netboot=true --set disable_iso=true --set disk.vhd=true --set disk.boot_active=true --set disk.size=8192 --set state_dir=/work/images > /work/build.log 2>&1 || { cat /work/build.log >&2; exit 1; }
fi
cd /work/images
if [ "$1" = iso ]; then set -- ./*.iso; else set -- ./*.vhd; fi
test "$#" -eq 1
test -s "$1"
tar --sparse --hole-detection=raw -cf - "$1"
'''
    print(f'Building private Kairos {image_format.upper()}…', flush=True)
    container = 'guaranine-build-' + secrets.token_hex(8)
    with (tempfile.TemporaryDirectory(prefix='build-', dir=directory, ignore_cleanup_errors=True) as scratch,
          archive.open('xb') as output, log.open('xb') as errors):
        try:
            result = subprocess.run(['sudo', '--', 'docker', 'run', '--rm', '--privileged', '--name', container,
                '--platform', 'linux/amd64', '-i', '--mount', f'type=bind,source={scratch},target=/work', '--mount',
                'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock',
                '--entrypoint', '/bin/bash', PINS['auroraboot'], '-ec', script, 'guaranine-builder',
                image_format, 'oci:' + source, str(os.getuid()), str(os.getgid())], input=cloud_yaml(config).encode(), stdout=output,
                stderr=errors, timeout=1800)
        finally:
            subprocess.run(['sudo', '--', 'docker', 'rm', '--force', container],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    archive.chmod(0o600)
    log.chmod(0o600)
    if result.returncode:
        raise ValueError(f'Kairos image build failed; inspect protected diagnostics in {log}')
    suffix = '.iso' if image_format == 'iso' else '.vhd'
    image = directory / ('guaranine-kairos' + suffix)
    with tarfile.open(archive) as package:
        members = package.getmembers()
        if (len(members) != 1 or not members[0].isfile() or members[0].size <= 0
                or not re.fullmatch(r'(\./)?[^/]+' + re.escape(suffix), members[0].name)):
            raise ValueError('The upstream builder must return exactly one image, without nested paths')
        package.extract(members[0], path=directory, filter='data')
        (directory / members[0].name).rename(image)
    archive.unlink()
    image.chmod(0o600)
    return image


def prepare_iso(_args):
    directory = operator_directory()
    settings = operator_settings()
    installer = directory / 'installer'
    key, config_path = installer / 'tailscale-key.json', installer / 'boot.yaml'
    image = installer / 'image/guaranine-kairos.iso'
    if image.exists() and not (image.parent / 'build-output.tar').exists():
        record = auth_record(key, reusable=True)
        protected(image)
    else:
        if installer.exists() and set(installer.iterdir()) != {key}:
            raise ValueError(f'{installer} contains an incomplete build; inspect its protected logs and revoke '
                             'the batch key before archiving it and starting a new batch')
        if not settings.get('laptop_models'):
            raise ValueError('Laptop models are pending; verify driver/firmware coverage before building laptop media')
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
        auth_record(key, reusable=True)
    print(f'Private shared ISO ready: {image}')
    expires = datetime.fromisoformat(record['expires'].replace('Z', '+00:00')).astimezone()
    print(f'New Tailscale registrations expire on {expires:%A, %B %-d, %Y at %-I:%M:%S %p %Z}.')
    flash_iso(image)
    print('Wi-Fi connects automatically. Run ./guaranine worker on the operator computer after booting the laptop.')


def prepare_controlplane(_args):
    directory = operator_directory()
    settings = operator_settings()
    node = directory / 'nodes/guaranine-controlplane'
    key, config_path = node / 'tailscale-key.json', node / 'config.yaml'
    image = node / 'image/guaranine-kairos.vhd'
    if image.exists() and not (image.parent / 'build-output.tar').exists():
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
            name='guaranine-controlplane', role='controlplane',
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
    scan = subprocess.run(['ssh-keyscan', '-T', '20', '-t', 'ed25519', address],
                          capture_output=True, text=True, check=True, timeout=30)
    lines = [line for line in scan.stdout.splitlines() if line and not line.startswith('#')]
    if len(lines) != 1:
        raise ValueError('Expected exactly one SSH Ed25519 host key')
    fields = lines[0].split()
    if len(fields) != 3 or fields[:2] != [address, 'ssh-ed25519']:
        raise ValueError('Unexpected SSH keyscan response')
    blob = base64.b64decode(fields[2], validate=True)
    if len(blob) != 51 or blob[:19] != b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':
        raise ValueError('Invalid SSH Ed25519 host key')
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    physical = input('SSH SHA256 fingerprint shown on the laptop or protected VM console: ').strip()
    if not secrets.compare_digest(physical, fingerprint):
        raise ValueError('SSH fingerprint mismatch; no remote command was sent')
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
    if (directory / 'kubeconfig').exists() or (directory / 'controlplane-known-hosts').exists():
        raise ValueError('Control-plane operator outputs already exist; inspect them before repeating bootstrap')
    address = tailnet_ip(input('Control-plane Tailscale IPv4 address: ').strip())
    with verified_connection(address, directory) as (connection, hosts):
        initialize_data(connection, directory)
        private_command([*connection, 'systemctl start var-lib-rancher-k3s.mount && '
            'mountpoint -q /var/lib/rancher/k3s && systemctl start --no-block k3s'], directory)
        raw = private_command([*connection, 'timeout 180 sh -ec \'until k3s kubectl wait --for=condition=Ready '
            'node/guaranine-controlplane --timeout=10s >/dev/null 2>&1; '
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


def install_worker(_args):
    directory = operator_directory()
    settings = operator_settings()
    cluster_endpoint = endpoint(settings.get('endpoint'))
    server = ssh_command(urllib.parse.urlsplit(cluster_endpoint).hostname, directory / 'controlplane-known-hosts')
    key = directory / 'installer/tailscale-key.json'
    auth_record(key, reusable=True)
    address = tailnet_ip(input('Laptop Tailscale IPv4 address: ').strip())
    with verified_connection(address, directory) as (connection, _hosts):
        live = private_command([*connection, 'test -f /run/cos/live_mode && '
            'test -d /run/rootfsbase && printf live'], directory)
        if live != 'live':
            raise ValueError('The laptop must be booted from the shared Kairos ISO')
        name = input('Unique lowercase worker name: ').strip()
        check_name(name)
        node = directory / 'nodes' / name
        if node.exists():
            raise ValueError('That worker already has private operator outputs; choose a unique name')
        if private_command([*server, 'k3s kubectl get node ' + name + ' --ignore-not-found -o name'], directory).strip():
            raise ValueError('That Kubernetes node already exists; retire and fence it before reusing its name')
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
        confirmation = f'ERASE {name} {disk}'
        print(f'Install {name} on {address}, destroying ALL DATA on {disk}.')
        if input(f'Type {confirmation} to install, or anything else to cancel: ').strip() != confirmation:
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
        command = 'umask 077; cat > /run/guaranine-install.yaml && kairos-agent manual-install --device '
        command += shlex.quote(disk) + ' --poweroff /run/guaranine-install.yaml'
        private_command([*connection, command], node, data=cloud_yaml(config), timeout=1800)
    print(f'Installation command completed for {name}. Remove the USB after power-off, then boot the laptop.')
    print(f'Its Tailscale address may change. Verify kubectl get node {name} reports Ready.')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='guaranine', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create private Kairos operator files once').set_defaults(
        action=lambda _args: initialize(operator_directory()))
    commands.add_parser('iso', help='Build/reuse a private Wi-Fi-capable ISO and offer USB flashing').set_defaults(action=prepare_iso)
    commands.add_parser('controlplane', help='Build/reuse the private Azure VHD').set_defaults(action=prepare_controlplane)
    commands.add_parser('bootstrap', help='Connect to the prepared cloud server and save its kubeconfig').set_defaults(action=bootstrap)
    commands.add_parser('worker', help='Choose a live laptop, worker name and installation disk').set_defaults(action=install_worker)
    os.umask(0o077)
    try:
        args = parser.parse_args(argv)
        args.action(args)
    except yaml.YAMLError:
        parser.exit(1, 'Invalid private YAML; check the operator/configuration file locally.\n')
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, 'Cancelled. If installation was already sent, check the laptop before retrying.\n')
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()

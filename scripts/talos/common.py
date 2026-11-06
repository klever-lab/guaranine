"""Pinned upstream tooling and protected local operator files."""
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((ROOT / 'talos/versions.json').read_text())
TAILNET = '100.64.0.0/10'


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

#!/usr/bin/env python3
"""Publish only the exact public Talos image with complete boot-test evidence."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from common import digest
from images import ISO, validate_public


def gh(*args, check=True):
    return subprocess.run(['gh', *args], check=check, text=True, stdout=subprocess.PIPE)


def publish(directory):
    directory = Path(directory)
    assets = [ISO, 'build.json', 'qemu-results.json', 'SHA256SUMS']
    if {path.name for path in directory.iterdir()} != set(assets):
        raise ValueError('Unexpected release files; refusing to publish private data')
    subprocess.run(['sha256sum', '--check', 'SHA256SUMS'], cwd=directory, check=True)
    manifest, evidence = validate_public(directory)
    if manifest.get('tests') != evidence:
        raise ValueError('The downloaded artifact has not been finalized with these test results')
    if manifest['source_dirty'] or manifest['source_commit'] != os.environ['EXPECTED_COMMIT']:
        raise ValueError('A release must match the exact clean tagged source')
    tag = os.environ['RELEASE_TAG']
    if not re.fullmatch(r'worker-iso-v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('Use a versioned worker ISO tag')
    existing = gh('release', 'view', tag, '--json', 'assets,isDraft', check=False)
    with tempfile.TemporaryDirectory(prefix='guaranine-release-') as scratch:
        scratch = Path(scratch)
        if existing.returncode:
            notes = scratch / 'notes.md'
            notes.write_text(f"Talos Linux public boot ISO from Guaranine `{manifest['source_commit']}`.\n\n"
                             "Includes the pinned Tailscale system extension. BIOS/UEFI boot, Talos maintenance API and preservation "
                             "of an unconfigured target disk were tested against these exact bytes.\n\n"
                             "Contains no enrollment keys or machine configuration. Private configuration and local image generation "
                             "are described in talos/README.md and images/laptop/README.md. "
                             "This release does not certify private enrollment, YubiKey authorization, hosted Tailscale or physical laptop hardware.\n\n"
                             "Supersedes the legacy Debian/K3s installer. Verify SHA256SUMS before use.\n")
            gh('release', 'create', tag, '--draft', '--verify-tag', '--target', manifest['source_commit'],
               '--title', f'Guaranine Talos {tag}', '--notes-file', str(notes))
            uploaded = set()
        else:
            info = json.loads(existing.stdout)
            uploaded = {item['name'] for item in info['assets']}
            if not uploaded <= set(assets):
                raise ValueError('Unexpected existing versioned assets')
            for name in sorted(uploaded):
                gh('release', 'download', tag, '--pattern', name, '--dir', str(scratch))
                if digest(scratch / name) != digest(directory / name):
                    raise ValueError('Versioned asset differs; reuse the original artifact or use a new tag')
        for name in assets:
            if name not in uploaded:
                gh('release', 'upload', tag, str(directory / name))
        gh('release', 'edit', tag, '--draft=false')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    publish(parser.parse_args().directory)

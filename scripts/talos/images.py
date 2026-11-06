#!/usr/bin/env python3
"""Build a private machine image locally with the pinned upstream Talos imager."""
import json
from pathlib import Path
import subprocess
import tarfile

import yaml

from common import PINS, digest, protected, output_directory, private_command, private_write, talosctl


def imager_profile(config, image_format):
    profile = {'baseProfileName': 'azure' if image_format == 'azure' else 'iso',
               'arch': 'amd64', 'version': PINS['talos'].removeprefix('v'),
               'platform': 'azure' if image_format == 'azure' else 'metal',
               'customization': {'embeddedMachineConfiguration': config},
               'input': {'systemExtensions': [{'imageRef': PINS['tailscale_extension']}],
                         'baseInstaller': {'imageRef': PINS['installer_base']}},
               'output': {'kind': 'iso' if image_format == 'iso' else 'image'}}
    if image_format == 'azure':
        profile['output'].update(imageOptions={'diskFormat': 'vhd', 'diskFormatOptions': 'subformat=fixed,force_size',
                                               'diskSize': 11736711168, 'bootloader': 'dual-boot'}, outFormat='.zst')
    return profile


def private_image(args):
    config = protected(args.config)
    documents = list(yaml.safe_load_all(config.read_text()))
    machine = next((doc for doc in documents if doc.get('version') == 'v1alpha1'), None)
    if not machine or machine.get('machine', {}).get('type') not in ('controlplane', 'worker'):
        raise ValueError('A complete private Talos machine configuration is required')
    if args.format == 'azure' and machine['machine']['type'] != 'controlplane':
        raise ValueError('Only the one planned control-plane VM is in scope for Azure images')
    if not any(doc.get('kind') == 'ExtensionServiceConfig' and doc.get('name') == 'tailscale' for doc in documents):
        raise ValueError('The image must include the Tailscale enrollment configuration')
    directory = output_directory(args.output)
    private_command([talosctl(), 'validate', '--strict', '--mode', 'metal', '--config', str(config)], directory)
    profile = yaml.safe_dump(imager_profile(config.read_text(), args.format), sort_keys=False)
    # Profile/credentials go through stdin, never shell arguments, environment,
    # public Image Factory, a Docker build context, or a repository file. Tar
    # output avoids root-owned bind-mount files from the privileged container.
    archive = directory / 'imager-output.tar'
    log = directory / 'imager.log'
    with archive.open('xb') as stream, log.open('xb') as errors:
        result = subprocess.run(['docker', 'run', '--rm', '--privileged', '--platform', 'linux/amd64', '-i', PINS['imager'], '-', '--tar-to-stdout'],
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
    private_write(directory / 'private.json', json.dumps({
        'private': True, 'format': args.format, 'talos_version': PINS['talos'],
        'machine_config_sha256': digest(config), 'images': {path.name: digest(path) for path in artifacts},
        'warning': 'Contains enrollment credentials. Never publish or upload as a CI artifact.'}, indent=2) + '\n')
    print('Built a private Talos machine image; keep the entire output directory private')
    return artifacts[0]

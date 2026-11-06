#!/usr/bin/env python3
"""Fetch the public Talos ISO, or build a private machine image locally."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tarfile

import yaml

from common import ROOT, PINS, digest, fetch, protected, output_directory, private_command, private_write, source, talosctl

ISO = 'guaranine-talos-amd64.iso'


def public_image(args):
    directory = output_directory(args.output)
    image = directory / ISO
    fetch(PINS['iso'], image)
    manifest = {'schema': 2, 'os': 'talos', 'private': False, **source(),
                'talos_version': PINS['talos'], 'kubernetes_version': PINS['kubernetes'],
                'schematic_id': PINS['schematic_id'], 'iso_sha256': digest(image),
                'upstream_url': PINS['iso']['url'],
                'qualification': 'Public image boot and maintenance API only; no credentials or automatic cluster enrollment',
                'tests': 'pending'}
    (directory / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Fetched the checksum-pinned public Talos ISO with the Tailscale extension')


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


def validate_public(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'build.json').read_text())
    evidence = json.loads((directory / 'qemu-results.json').read_text())
    if manifest.get('private') is not False or (directory / 'private.json').exists():
        raise ValueError('Private machine images can never be released')
    if manifest['os'] != 'talos' or manifest['iso_sha256'] != digest(directory / ISO) or manifest['iso_sha256'] != PINS['iso']['sha256']:
        raise ValueError('The image does not match the Talos build manifest')
    if evidence['iso_sha256'] != manifest['iso_sha256'] or evidence['source_commit'] != manifest['source_commit']:
        raise ValueError('Guest evidence belongs to another image/source commit')
    if set(evidence['firmware']) != {'bios', 'uefi'}:
        raise ValueError('Both BIOS and UEFI boot evidence are required')
    for result in evidence['firmware'].values():
        if result != {'boot': 'passed', 'maintenance_api': 'passed', 'disk_unchanged': 'passed', 'talos_version': PINS['talos']}:
            raise ValueError('A required public-image test failed or is missing')
    return manifest, evidence


def finalize(directory):
    directory = Path(directory)
    manifest, evidence = validate_public(directory)
    manifest['tests'] = evidence
    (directory / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (directory / 'SHA256SUMS').write_text(''.join(f'{digest(directory/name)}  {name}\n' for name in (ISO, 'build.json', 'qemu-results.json')))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    public = commands.add_parser('public', help='Fetch the public, credential-free ISO')
    public.add_argument('--output', required=True)
    public.set_defaults(action=public_image)
    private = commands.add_parser('private', help='Build a private ISO or Azure VHD locally using upstream imager')
    private.add_argument('--config', required=True)
    private.add_argument('--format', choices=('iso', 'azure'), default='iso')
    private.add_argument('--output', required=True)
    private.set_defaults(action=private_image)
    final = commands.add_parser('finalize')
    final.add_argument('directory')
    final.set_defaults(action=lambda args: finalize(args.directory))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        args.action(args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f'{type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()

"""Execute both bootstrap recipes with signed-index and package-download fixtures."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
RECIPES = ('images/laptop/Dockerfile', 'scripts/iso/Test.Dockerfile')
CONTENT = b'DISPOSABLE DEBIAN PACKAGE FIXTURE'
DIGEST = hashlib.sha256(CONTENT).hexdigest()


class BootstrapMirrorTests(unittest.TestCase):
    def bootstrap(self, recipe, failed_indexes=(), failed_downloads=(), corrupt_downloads=(),
                  package_hash='SHA256:' + DIGEST):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ('etc/ssl/certs', 'etc/apt/sources.list.d', 'tmp', 'var/lib/apt/lists', 'bin'):
                (root / path).mkdir(parents=True, exist_ok=True)
            (root / 'etc/ssl/certs/ca-certificates.crt').write_text('DISPOSABLE PUBLIC CA FIXTURE')
            (root / 'etc/apt/sources.list.d/debian.sources').write_text('DISPOSABLE SOURCE FIXTURE')
            stub = root / 'bin/apt-get'
            stub.write_text(f'#!{sys.executable}\n' + '''import hashlib, json, os, pathlib, sys
from urllib.parse import urlsplit
root = pathlib.Path(os.environ['APT_FIXTURE'])
args = sys.argv[1:]
source = (root / 'etc/apt/sources.list').read_text().strip()
host = source.split('https://', 1)[1].split('/', 1)[0]
content = b'DISPOSABLE DEBIAN PACKAGE FIXTURE'
if sys.argv[0].endswith('apt-helper'):
    url, filename, expected = args[-3:]
    host = urlsplit(url).hostname
    stage = 'download'
else:
    stage = 'update' if 'update' in args else 'plan' if '--print-uris' in args else 'install'
with (root / 'calls').open('a') as stream:
    stream.write(json.dumps({'source':source, 'host':host, 'stage':stage, 'args':args}) + '\\n')
if stage == 'update':
    raise SystemExit(100 if host in json.loads(os.environ['FAILED_INDEXES']) else 0)
if stage == 'plan':
    assert 'Acquire::ForceHash=SHA256' in args
    base = source.split()[2].rstrip('/')
    print('Reading package lists...')
    for name in ('fixture-a_1_amd64.deb', 'fixture-b_1_amd64.deb'):
        print("'%s/pool/main/f/fixture/%s' %s %d %s" %
              (base, name, name, len(content), os.environ['PACKAGE_HASH']))
elif stage == 'download':
    if host in json.loads(os.environ['FAILED_DOWNLOADS']):
        raise SystemExit(100)
    received = b'CORRUPT CONTENT' if host in json.loads(os.environ['CORRUPT_DOWNLOADS']) else content
    if expected != 'SHA256:' + hashlib.sha256(received).hexdigest():
        raise SystemExit(100)
    pathlib.Path(filename).write_bytes(received)
else:
    assert '--no-download' in args
    for name in ('fixture-a_1_amd64.deb', 'fixture-b_1_amd64.deb'):
        assert (root / 'var/cache/apt/archives' / name).read_bytes() == content
''')
            stub.chmod(0o755)
            (root / 'bin/apt-helper').symlink_to(stub)
            text = (ROOT / recipe).read_text()
            command = re.search(r'(?ms)^RUN (.*?)(?=^[A-Z][A-Z_]*[ \t]|\Z)', text)[1]
            command = command.replace('\\\n', ' ')
            command = re.sub(r'/(etc|tmp|var/lib/apt|var/cache/apt)/',
                             lambda m: str(root / m[1]) + '/', command)
            command = command.replace('/usr/lib/apt/apt-helper', str(root / 'bin/apt-helper'))
            environment = {**os.environ, 'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH'],
                           'APT_FIXTURE': str(root), 'FAILED_INDEXES': json.dumps(failed_indexes),
                           'FAILED_DOWNLOADS': json.dumps(failed_downloads),
                           'CORRUPT_DOWNLOADS': json.dumps(corrupt_downloads), 'PACKAGE_HASH': package_hash}
            result = subprocess.run(['/bin/sh', '-eu', '-c', command], env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertTrue((root / 'calls').exists(), result.stderr)
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
            snapshot = json.loads((ROOT / 'images/laptop/inputs.json').read_text())['debian']['snapshot']
            for call in calls:
                self.assertIn('/archive/debian/' + snapshot + ' trixie main', call['source'])
                self.assertNotIn('trusted=yes', call['source'])
                self.assertNotIn('--allow-unauthenticated', call['args'])
                for option, maximum in (('Acquire::Retries=', 5), ('Acquire::https::Timeout=', 60)):
                    values = [int(arg.removeprefix(option)) for arg in call['args'] if arg.startswith(option)]
                    self.assertEqual(len(values), 1)
                    self.assertTrue(0 < values[0] <= maximum)
                if call['stage'] == 'download':
                    self.assertEqual(call['args'][-1], 'SHA256:' + DIGEST)
            return result, calls

    def test_snapshot_package_outage_uses_exact_cdn_bytes(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe):
                result, calls = self.bootstrap(recipe, failed_downloads=[
                    'snapshot.debian.org', 'snapshot-cloudflare.debian.org'])
                self.assertEqual(result.returncode, 0, result.stderr)
                downloads = [c for c in calls if c['stage'] == 'download']
                self.assertEqual([c['host'] for c in downloads], ['deb.debian.org'] * 2)
                self.assertEqual(calls[-1]['stage'], 'install')

    def test_index_outage_uses_same_pinned_snapshot_on_second_mirror(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe):
                result, calls = self.bootstrap(recipe, failed_indexes=['snapshot.debian.org'])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([c['host'] for c in calls[:2]],
                                 ['snapshot.debian.org', 'snapshot-cloudflare.debian.org'])
                self.assertEqual(calls[-1]['host'], 'snapshot-cloudflare.debian.org')

    def test_cdn_outage_falls_back_to_original_snapshot_uri(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe):
                result, calls = self.bootstrap(recipe, failed_downloads=['deb.debian.org'])
                self.assertEqual(result.returncode, 0, result.stderr)
                downloads = [c for c in calls if c['stage'] == 'download']
                self.assertEqual([c['host'] for c in downloads],
                                 ['deb.debian.org', 'snapshot.debian.org'] * 2)

    def test_corrupt_cdn_content_cannot_be_installed(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe):
                result, calls = self.bootstrap(recipe, corrupt_downloads=['deb.debian.org'])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len([c for c in calls if c['stage'] == 'download']), 4)
                self.assertEqual(calls[-1]['stage'], 'install')

    def test_unavailable_package_sources_fail_before_installing(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe):
                result, calls = self.bootstrap(recipe, failed_downloads=[
                    'deb.debian.org', 'snapshot.debian.org', 'snapshot-cloudflare.debian.org'])
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('install', [c['stage'] for c in calls])

    def test_weak_missing_or_malformed_hash_cannot_download_or_install(self):
        for recipe in RECIPES:
            for invalid in ('MD5Sum:' + 'a' * 32, '', 'SHA256:' + 'g' * 64, 'SHA256:abcd'):
                with self.subTest(recipe=recipe, invalid=invalid):
                    result, calls = self.bootstrap(recipe, package_hash=invalid)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse({'download', 'install'} & {c['stage'] for c in calls})

    def test_unavailable_signed_indexes_fail_without_downloading(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe):
                result, calls = self.bootstrap(recipe, failed_indexes=[
                    'snapshot.debian.org', 'snapshot-cloudflare.debian.org'])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([c['stage'] for c in calls], ['update', 'update'])


if __name__ == '__main__':
    unittest.main()

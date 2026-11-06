"""Execute both bootstrap recipes against disposable APT mirror fixtures."""

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


class BootstrapMirrorTests(unittest.TestCase):
    def bootstrap(self, recipe, unavailable=(), failure_stage='all'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ('etc/ssl/certs', 'etc/apt/sources.list.d', 'tmp', 'var/lib/apt/lists', 'bin'):
                (root / path).mkdir(parents=True, exist_ok=True)
            (root / 'etc/ssl/certs/ca-certificates.crt').write_text('DISPOSABLE PUBLIC CA FIXTURE')
            (root / 'etc/apt/sources.list.d/debian.sources').write_text('DISPOSABLE SOURCE FIXTURE')
            stub = root / 'bin/apt-get'
            stub.write_text(f'#!{sys.executable}\n' + '''import json, os, pathlib, sys
root = pathlib.Path(os.environ['APT_FIXTURE'])
source = (root / 'etc/apt/sources.list').read_text().strip()
host = source.split('https://', 1)[1].split('/', 1)[0]
stage = 'install' if 'install' in sys.argv else 'update'
with (root / 'calls').open('a') as stream:
    stream.write(json.dumps({'source':source,'host':host,'stage':stage,'args':sys.argv[1:]}) + '\\n')
failed = host in json.loads(os.environ['FAILED_HOSTS']) and os.environ['FAILURE_STAGE'] in {'all', stage}
raise SystemExit(100 if failed else 0)
''')
            stub.chmod(0o755)
            text = (ROOT / recipe).read_text()
            command = re.search(r'(?ms)^RUN (.*?)(?=^[A-Z][A-Z_]*[ \t]|\Z)', text)[1]
            command = command.replace('\\\n', ' ')
            command = re.sub(r'/(etc|tmp|var/lib/apt)/', lambda m: str(root / m[1]) + '/', command)
            environment = {**os.environ, 'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH'],
                           'APT_FIXTURE': str(root), 'FAILED_HOSTS': json.dumps(unavailable),
                           'FAILURE_STAGE': failure_stage}
            result = subprocess.run(['/bin/sh', '-eu', '-c', command], env=environment,
                                    capture_output=True, text=True, timeout=10)
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
            snapshot = json.loads((ROOT / 'images/laptop/inputs.json').read_text())['debian']['snapshot']
            for call in calls:
                self.assertIn('/archive/debian/' + snapshot + ' trixie main', call['source'])
                self.assertNotIn('trusted=yes', call['source'])
                self.assertNotIn('--allow-unauthenticated', call['args'])
                if result.returncode == 0:
                    for option, maximum in (('Acquire::Retries=', 5), ('Acquire::https::Timeout=', 60)):
                        values = [int(arg.removeprefix(option)) for arg in call['args'] if arg.startswith(option)]
                        self.assertEqual(len(values), 1)
                        self.assertTrue(0 < values[0] <= maximum)
            return result, calls

    def test_cloudflare_outage_does_not_affect_canonical_snapshot_bootstrap(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe):
                result, calls = self.bootstrap(recipe, ['snapshot-cloudflare.debian.org'])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([c['host'] for c in calls], ['snapshot.debian.org'] * 2)

    def test_index_and_package_failures_fall_back_to_the_same_pinned_snapshot(self):
        for recipe in RECIPES:
            for stage in ('update', 'install'):
                with self.subTest(recipe=recipe, stage=stage):
                    result, calls = self.bootstrap(recipe, ['snapshot.debian.org'], stage)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(calls[0]['host'], 'snapshot.debian.org')
                    self.assertEqual(calls[-1]['host'], 'snapshot-cloudflare.debian.org')
                    self.assertEqual(calls[-1]['stage'], 'install')

    def test_unavailable_mirrors_fail_the_build(self):
        for recipe in RECIPES:
            with self.subTest(recipe=recipe):
                result, calls = self.bootstrap(recipe, ['snapshot.debian.org', 'snapshot-cloudflare.debian.org'])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual([c['host'] for c in calls], ['snapshot.debian.org', 'snapshot-cloudflare.debian.org'])


if __name__ == '__main__':
    unittest.main()

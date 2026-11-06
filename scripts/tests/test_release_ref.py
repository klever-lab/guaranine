"""Release authorization accepts direct main pushes with exact-commit CI evidence."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = 'fixture/guaranine'


class ReleaseRefTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.git('init', '--initial-branch=main')
        self.git('config', 'user.name', 'Release fixture')
        self.git('config', 'user.email', 'release@example.invalid')
        (self.directory / 'source').write_text('tested source\n')
        self.git('add', 'source')
        self.git('commit', '-m', 'Accepted source')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.head = self.commit
        self.git('update-ref', 'refs/remotes/origin/main', self.commit)
        self.checks = [{'check_runs': [
            {'id': index, 'name': name, 'head_sha': self.head, 'app': {'slug': 'github-actions'},
             'status': 'completed', 'conclusion': 'success'}
            for index, name in enumerate(('Clusterfork validation', 'trust', 'build-test'), start=10)]}]
        self.bin = self.directory / 'bin'
        self.bin.mkdir()
        stub = self.bin / 'gh'
        stub.write_text(f'#!{sys.executable}\n' + '''import json, os, pathlib, sys
assert sys.argv[1:4] == ['api', '--paginate', '--slurp']
url = sys.argv[4]
root = pathlib.Path(os.environ['RELEASE_FIXTURE'])
with (root / 'calls').open('a') as stream:
    stream.write(url + '\\n')
fixture = json.loads((root / 'responses.json').read_text())
if url not in fixture:
    raise SystemExit('Unexpected release API operation')
print(json.dumps(fixture[url]))
''')
        stub.chmod(0o755)

    def git(self, *arguments):
        return subprocess.check_output(['git', *arguments], cwd=self.directory,
                                       stderr=subprocess.DEVNULL, text=True)

    def guard(self, ref='refs/tags/worker-iso-v0.1.0'):
        fixture = {
            f'repos/{REPOSITORY}/commits/{self.head}/check-runs?filter=all&per_page=100': self.checks,
        }
        (self.directory / 'responses.json').write_text(json.dumps(fixture))
        environment = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                       'RELEASE_REF': ref, 'GH_REPO': REPOSITORY, 'GH_TOKEN': 'disposable-fixture',
                       'RELEASE_FIXTURE': str(self.directory)}
        return subprocess.run(['bash', str(ROOT / 'scripts/talos/check-release-ref.sh')],
                              cwd=self.directory, env=environment, capture_output=True, text=True)

    def test_direct_main_commit_with_passing_ci_needs_no_pr_or_review_api(self):
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.directory / 'calls').read_text().splitlines(), [
            f'repos/{REPOSITORY}/commits/{self.head}/check-runs?filter=all&per_page=100'])

    def test_missing_pending_failed_foreign_or_untrusted_checks_cannot_release(self):
        for name in ('Clusterfork validation',):
            for mutation in ('missing', 'pending', 'failed', 'foreign', 'untrusted'):
                with self.subTest(name=name, mutation=mutation):
                    checks = copy.deepcopy(self.checks)
                    target = next(c for c in self.checks[0]['check_runs'] if c['name'] == name)
                    if mutation == 'missing':
                        self.checks[0]['check_runs'].remove(target)
                    elif mutation == 'pending':
                        target.update(status='in_progress', conclusion=None)
                    elif mutation == 'failed':
                        target['conclusion'] = 'failure'
                    elif mutation == 'foreign':
                        target['head_sha'] = 'b' * 40
                    else:
                        target['app']['slug'] = 'untrusted-check-writer'
                    self.assertNotEqual(self.guard().returncode, 0)
                    self.checks = checks

    def test_latest_check_result_controls_reruns(self):
        previous = copy.deepcopy(self.checks[0]['check_runs'][0])
        latest = {**previous, 'id': 100, 'conclusion': 'failure'}
        self.checks.append({'check_runs': [latest]})
        self.assertNotEqual(self.guard().returncode, 0)
        latest['conclusion'] = 'success'
        self.checks[0]['check_runs'][0]['conclusion'] = 'failure'
        self.assertEqual(self.guard().returncode, 0)

    def test_prior_pr_image_checks_are_not_required_to_authorize_a_fresh_tag_build(self):
        # Documentation-only PRs need not trigger the path-filtered ISO workflow.
        # The release workflow itself still requires its exact-image build/test.
        self.checks[0]['check_runs'] = self.checks[0]['check_runs'][:1]
        self.assertEqual(self.guard().returncode, 0)

    def test_unversioned_refs_and_commits_outside_main_fail_before_network_access(self):
        for ref in ('refs/heads/main', 'refs/tags/preview', 'refs/tags/worker-iso-v1.2'):
            with self.subTest(ref=ref):
                self.assertNotEqual(self.guard(ref).returncode, 0)
                self.assertFalse((self.directory / 'calls').exists())
        (self.directory / 'source').write_text('unmerged change\n')
        self.git('commit', '-am', 'Unmerged source')
        self.assertNotEqual(self.guard().returncode, 0)
        self.assertFalse((self.directory / 'calls').exists())


if __name__ == '__main__':
    unittest.main()

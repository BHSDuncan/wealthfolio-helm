"""Exercise the actual publication shell against a synthetic registry boundary."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicationRetryTests(unittest.TestCase):
    def run_publication(self, *, existing=None, public=True):
        workflow = (ROOT / '.github/workflows/publish.yml').read_text()
        step = workflow.split('      - name: Publish or verify an exact retry, then check anonymous pullback\n', 1)[1]
        script = textwrap.dedent(step.split('        run: |\n', 1)[1].split('\n  github-release:', 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            binaries = target / '.tools/bin'
            binaries.mkdir(parents=True)
            dist = target / 'dist'
            dist.mkdir()
            candidate = dist / 'wealthfolio-0.1.0.tgz'
            candidate.write_bytes(b'synthetic-authorized-chart')
            candidate.with_suffix('.tgz.sha256').write_text(hashlib.sha256(candidate.read_bytes()).hexdigest() + '  ' + candidate.name + '\n')
            remote = target / 'remote.tgz'
            if existing is not None:
                remote.write_bytes(existing)
            checker = target / '.github/scripts/verify_checksum.py'
            checker.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / '.github/scripts/verify_checksum.py', checker)
            probe = binaries / 'python3'
            probe.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
                import os,pathlib,subprocess,sys
                if sys.argv[1:] == ['-']:
                    sys.stdin.read()
                    if pathlib.Path('remote.tgz').exists():
                        pathlib.Path(os.environ['RUNNER_TEMP'], 'existing-oci-version').touch()
                else:
                    raise SystemExit(subprocess.call([sys.executable, *sys.argv[1:]]))
                '''))
            helm = binaries / 'helm'
            helm.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''\
                import os,pathlib,shutil,sys
                args=sys.argv[1:]
                with open('helm-calls', 'a') as output: output.write(' '.join(args[:2])+'\\n')
                if args[0]=='push': shutil.copyfile(args[1], 'remote.tgz')
                elif args[0]=='pull':
                    destination=pathlib.Path(args[args.index('--destination')+1])
                    shutil.copyfile('remote.tgz', destination/'wealthfolio-0.1.0.tgz')
                elif args[:2]==['show','chart'] and os.environ['TEST_PUBLIC']!='true':
                    raise SystemExit(1)
                '''))
            probe.chmod(0o700)
            helm.chmod(0o700)
            runner = target / 'runner'
            runner.mkdir()
            environment = dict(os.environ, RUNNER_TEMP=str(runner), OWNER='Example', VERSION='0.1.0',
                               GH_TOKEN='synthetic-test-only', GHCR_USER='synthetic', TEST_PUBLIC=str(public).lower())
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=target,
                                    env=environment, capture_output=True, text=True)
            calls = (target / 'helm-calls').read_text().splitlines()
            return result, calls

    def test_first_publication_pushes_then_checks_pullback(self):
        result, calls = self.run_publication()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(call.startswith('push ') for call in calls), 1)

    def test_exact_existing_artifact_retries_without_push(self):
        result, calls = self.run_publication(existing=b'synthetic-authorized-chart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call.startswith('push ') for call in calls))
        self.assertIn('show chart', calls)

    def test_different_existing_artifact_is_never_overwritten(self):
        result, calls = self.run_publication(existing=b'different-chart')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('overwrite forbidden', result.stderr)
        self.assertFalse(any(call.startswith('push ') for call in calls))
        self.assertNotIn('show chart', calls)

    def test_matching_private_artifact_still_fails_anonymous_check(self):
        result, calls = self.run_publication(existing=b'synthetic-authorized-chart', public=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith('push ') for call in calls))
        self.assertIn('show chart', calls)

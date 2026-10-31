"""Exercise owner authorization before every publication job, including retries."""
import os
from pathlib import Path
import re
import subprocess
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicationOwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/publish.yml').read_text()

    def check_authorization(self, *, actor='ChartOwner', triggering_actor='ChartOwner',
                            event='workflow_dispatch', allowed):
        for job_name in ('gate', 'oci', 'github-release'):
            with self.subTest(job=job_name):
                # The guard must run before checkout or any step with write tokens.
                job = self.workflow.split(f'\n  {job_name}:\n', 1)[1]
                job = re.split(r'\n  [a-z-]+:\n', job, maxsplit=1)[0]
                steps = job.split('    steps:\n', 1)[1]
                marker = '      - name: Require repository owner\n'
                self.assertTrue(steps.startswith(marker))
                guard = steps[len(marker):].split('\n      - ', 1)[0]
                env_block, script = guard.split('        run: |\n', 1)
                env_lines = env_block.split('        env:\n', 1)[1].strip().splitlines()
                guard_env = dict(line.strip().split(': ', 1) for line in env_lines)
                self.assertEqual(guard_env, {
                    'REPOSITORY_OWNER': '${{ github.repository_owner }}',
                    'ACTOR': '${{ github.actor }}',
                    'TRIGGERING_ACTOR': '${{ github.triggering_actor }}',
                })
                environment = dict(os.environ, REPOSITORY_OWNER='ChartOwner', ACTOR=actor,
                                   TRIGGERING_ACTOR=triggering_actor, GITHUB_EVENT_NAME=event)
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', textwrap.dedent(script)],
                                        env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, allowed, result.stderr)

    def test_owner_can_dispatch_and_rerun(self):
        self.check_authorization(allowed=True)

    def test_github_login_case_does_not_change_identity(self):
        self.check_authorization(actor='chartowner', triggering_actor='CHARTOWNER', allowed=True)

    def test_nonowner_cannot_dispatch(self):
        self.check_authorization(actor='Collaborator', triggering_actor='Collaborator', allowed=False)

    def test_nonowner_cannot_rerun_an_owners_job(self):
        self.check_authorization(triggering_actor='Collaborator', allowed=False)

    def test_owner_rerun_does_not_authorize_another_actors_dispatch(self):
        self.check_authorization(actor='Collaborator', allowed=False)

    def test_other_event_cannot_publish(self):
        self.check_authorization(event='push', allowed=False)

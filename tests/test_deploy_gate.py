"""Untrusted GitHub facts never authorize cloud credentials (offline only)."""

from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import deploy_gate as gate
from scripts.deploy_support import DeployError


SHA = 'a' * 40
OTHER = 'b' * 40
REPO = 'gorpsys/zontcontrol'


def run_fact(**changes):
    result = {'id': 31, 'name': 'Project checks', 'path': '.github/workflows/checks.yml',
              'event': 'push', 'head_branch': 'master', 'head_sha': SHA,
              'status': 'completed', 'conclusion': 'success',
              'repository': {'full_name': REPO}, 'head_repository': {'full_name': REPO}}
    result.update(changes)
    return result


def environment(**changes):
    result = {'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/master',
              'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_SHA': SHA}
    result.update(changes)
    return result


def checks():
    return [{'name': name, 'head_sha': SHA, 'app': {'slug': 'github-actions', 'id': 15368},
             'status': 'completed', 'conclusion': 'success',
             'details_url': f'https://github.com/{REPO}/actions/runs/31/job/{i}'}
            for i, name in enumerate(('Lint', 'Offline checks'), 1)]


class FakeGitHub:
    def __init__(self):
        self.heads = [SHA, SHA]
        self.items = checks()
        self.run = run_fact()

    def master_sha(self):
        return self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]

    def checks(self, sha):
        assert sha == SHA
        return deepcopy(self.items)

    def workflow_run(self, run_id):
        assert run_id == '31'
        return deepcopy(self.run)


class GateTests(unittest.TestCase):
    def test_github_http_requests_name_the_checks_boundary(self):
        with patch.object(gate, 'request_json', return_value={}) as request:
            gate.GitHub('synthetic-private').read('branches/master')
        self.assertEqual(request.call_args.kwargs, {'stage': 'GitHub checks API'})

    def test_only_checked_repository_master_events(self):
        self.assertEqual(gate.select_sha({'workflow_run': run_fact()}, environment()), SHA)
        self.assertEqual(gate.select_sha({}, environment(GITHUB_EVENT_NAME='workflow_dispatch')), SHA)
        for field, value in [('event', 'pull_request'), ('head_branch', 'feature'),
                             ('conclusion', 'failure'), ('name', 'Other checks'),
                             ('path', '.github/workflows/other.yml'),
                             ('head_repository', {'full_name': 'fork/zontcontrol'})]:
            with self.subTest(field=field), self.assertRaises(DeployError):
                gate.select_sha({'workflow_run': run_fact(**{field: value})}, environment())
        for changes in [{'GITHUB_REPOSITORY': 'fork/zontcontrol'},
                        {'GITHUB_REF': 'refs/heads/feature'},
                        {'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_SHA': 'short',
                         'GITHUB_EVENT_NAME': 'workflow_dispatch'}]:
            with self.subTest(changes=changes), self.assertRaises(DeployError):
                gate.select_sha({'workflow_run': run_fact()}, environment(**changes))

    def test_trusted_checks_and_current_head_are_required(self):
        self.assertTrue(gate.wait_ready(FakeGitHub(), SHA, 0))
        for key, value in [('app', {'slug': 'other', 'id': 15368}),
                           ('app', {'slug': 'github-actions', 'id': 99}),
                           ('head_sha', OTHER), ('conclusion', 'failure'),
                           ('details_url', f'https://evil.invalid/{REPO}/actions/runs/31/job/1'),
                           ('details_url', 'https://github.com/fork/zontcontrol/actions/runs/31/job/1')]:
            client = FakeGitHub()
            client.items[0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(DeployError):
                gate.wait_ready(client, SHA, 0)

    def test_run_identity_is_checked_in_addition_to_check_name(self):
        for key, value in [('path', '.github/workflows/fake.yml'), ('event', 'pull_request'),
                           ('head_sha', OTHER), ('head_branch', 'feature'),
                           ('repository', {'full_name': 'fork/zontcontrol'}),
                           ('head_repository', {'full_name': 'fork/zontcontrol'}),
                           ('conclusion', 'failure')]:
            client = FakeGitHub()
            client.run[key] = value
            with self.subTest(key=key), self.assertRaises(DeployError):
                gate.wait_ready(client, SHA, 0)

    def test_missing_pending_duplicate_or_status_only_cannot_pass(self):
        for items in ([], [checks()[0]], checks() + [checks()[0]],
                      [dict(item, status='in_progress') for item in checks()]):
            client = FakeGitHub()
            client.items = items
            with self.subTest(items=items), self.assertRaises(DeployError):
                gate.wait_ready(client, SHA, 0)

    def test_stale_head_and_race_during_check_read_are_rejected(self):
        for heads in ([OTHER], [SHA, OTHER]):
            client = FakeGitHub()
            client.heads = list(heads)
            self.assertFalse(gate.wait_ready(client, SHA, 0))

    def test_check_once_refuses_a_different_pinned_sha(self):
        with TemporaryDirectory() as directory:
            event = Path(directory) / 'event.json'
            event.write_text('{}')
            env = environment(GITHUB_EVENT_NAME='workflow_dispatch', DEPLOY_SHA=OTHER,
                              GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(event.parent / 'output'))
            with patch.dict(os.environ, env, clear=True), patch.object(gate, 'GitHub') as github:
                self.assertEqual(gate.main(['--check-once']), 1)
                github.assert_not_called()

    def test_pagination_does_not_hide_missing_or_failed_checks(self):
        client = gate.GitHub('synthetic-github-credential')
        with patch.object(client, 'read', side_effect=[{
                'total_count': 101, 'check_runs': [{}] * 100},
                {'total_count': 101, 'check_runs': checks()[1:]}]):
            self.assertEqual(len(client.checks(SHA)), 101)
        with patch.object(client, 'read', return_value={'total_count': 3, 'check_runs': []}):
            with self.assertRaises(DeployError):
                client.checks(SHA)

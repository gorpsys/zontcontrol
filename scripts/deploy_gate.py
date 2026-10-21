"""Authorize only trusted successful checks for the current repository master."""

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.request import Request

from scripts.deploy_support import DeployError, commit_sha, record, request_json, text


REPOSITORY = 'gorpsys/zontcontrol'
REQUIRED_CHECKS = ('Lint', 'Offline checks')
WORKFLOW_PATH = '.github/workflows/checks.yml'
POLL_SECONDS = 15


def validate_run(value: object, sha: str) -> None:
    run = record(value)
    expected = {'name': 'Project checks', 'path': WORKFLOW_PATH, 'event': 'push',
                'head_branch': 'master', 'head_sha': sha,
                'status': 'completed', 'conclusion': 'success'}
    if any(run.get(key) != item for key, item in expected.items()) or any(
            record(run.get(key)).get('full_name') != REPOSITORY
            for key in ('repository', 'head_repository')):
        raise DeployError('untrusted checks workflow run')


def select_sha(event: object, environment: Mapping[str, str]) -> str:
    if environment.get('GITHUB_REPOSITORY') != REPOSITORY or (
            environment.get('GITHUB_REF') != 'refs/heads/master'):
        raise DeployError('deployment requires repository master')
    name = environment.get('GITHUB_EVENT_NAME')
    if name == 'workflow_dispatch':
        return commit_sha(environment.get('GITHUB_SHA'))
    if name != 'workflow_run':
        raise DeployError('unsupported deployment event')
    run = record(record(event).get('workflow_run'))
    sha = commit_sha(run.get('head_sha'))
    validate_run(run, sha)
    return sha


class GitHub:
    """Read-only requests to a fixed repository; no cloud credentials required."""

    def __init__(self, token: str) -> None:
        self.token = text(token, 'GitHub token')

    def read(self, route: str) -> object:
        return request_json(Request(f'https://api.github.com/repos/{REPOSITORY}/{route}',
            headers={'Authorization': f'Bearer {self.token}',
                     'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28'}), stage='GitHub checks API')

    def master_sha(self) -> str:
        return commit_sha(record(record(self.read('branches/master')).get('commit')).get('sha'))

    def workflow_run(self, run_id: str) -> object:
        if re.fullmatch('[1-9][0-9]*', run_id) is None:
            raise DeployError('invalid checks run ID')
        return self.read(f'actions/runs/{run_id}')

    def checks(self, sha: str) -> list[object]:
        result = []
        for page in range(1, 21):
            data = record(self.read(
                f'commits/{commit_sha(sha)}/check-runs?filter=latest&per_page=100&page={page}'))
            count, checks = data.get('total_count'), data.get('check_runs')
            if type(count) is not int or count < 0 or not isinstance(checks, list) or len(checks) > 100:
                raise DeployError('invalid check-run pagination')
            result.extend(checks)
            if len(result) == count:
                return result
            if not checks or len(result) > count:
                raise DeployError('incomplete check-run pagination')
        raise DeployError('too many check-run pages')


def pending_checks(client: GitHub, checks: Sequence[object], sha: str) -> tuple[str, ...]:
    """Commit statuses cannot substitute for the named GitHub Actions checks."""
    pending, runs = [], {}
    for name in REQUIRED_CHECKS:
        selected = [record(item) for item in checks if record(item).get('name') == name]
        if not selected:
            pending.append(name)
            continue
        if len(selected) != 1:
            raise DeployError('ambiguous required check')
        check = selected[0]
        app = record(check.get('app'))
        if check.get('head_sha') != sha or app.get('slug') != 'github-actions' or app.get('id') != 15368:
            raise DeployError('untrusted required check producer or SHA')
        match = re.fullmatch(r'https://github\.com/' + REPOSITORY
                            + r'/actions/runs/([1-9][0-9]*)/job/[1-9][0-9]*',
                            str(check.get('details_url', '')))
        if match is None:
            raise DeployError('untrusted required check origin')
        if check.get('status') in ('queued', 'in_progress', 'waiting', 'pending', 'requested'):
            pending.append(name)
            continue
        if check.get('status') != 'completed' or check.get('conclusion') != 'success':
            raise DeployError('required check failed')
        run_id = match[1]
        if run_id not in runs:
            runs[run_id] = client.workflow_run(run_id)
        run = record(runs[run_id])
        if str(run.get('id')) != run_id:
            raise DeployError('checks workflow run identity mismatch')
        validate_run(run, sha)
    return tuple(pending)


def wait_ready(client: GitHub, sha: str, wait_seconds: float, *,
               clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = clock() + wait_seconds
    while True:
        if client.master_sha() != sha:
            return False
        pending = pending_checks(client, client.checks(sha), sha)
        if not pending:
            return client.master_sha() == sha
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeployError('required checks timeout')
        sleep(min(POLL_SECONDS, remaining))


def authorize(environment: Mapping[str, str], *, wait_seconds: float = 0) -> str:
    """Revalidate the pinned commit immediately before a credential exchange."""
    event = json.loads(Path(environment['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    sha = select_sha(event, environment)
    if environment.get('DEPLOY_SHA') and commit_sha(environment['DEPLOY_SHA']) != sha:
        raise DeployError('pinned deployment SHA differs from event')
    if not wait_ready(GitHub(environment.get('GH_TOKEN', '')), sha, wait_seconds):
        raise DeployError('master changed before cloud publication')
    return sha


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-once', action='store_true')
    args = parser.parse_args(argv)
    try:
        sha = authorize(dict(os.environ), wait_seconds=0 if args.check_once else 600)
        with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
            output.write(f'sha={sha}\nready=true\n')
        print(f'Checked current master {sha}', flush=True)
    except DeployError as error:
        print(f'Deployment gate rejected: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError):
        print('Deployment gate configuration invalid', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

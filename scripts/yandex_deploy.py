"""Publish a checked once ZIP through short-lived OIDC; never invoke the function."""

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from scripts.build_yandex_archive import ArchiveError, verify_archive
from scripts.deploy_gate import REPOSITORY, authorize
from scripts.deploy_support import (
    MAX_RESPONSE_BYTES, DeployError, commit_sha, record, request_json, text,
)
from zontcontrol.config import ConfigurationError, load_controller_config


def identifier(value: object, label: str) -> str:
    result = text(value, label)
    if re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*', result) is None:
        raise DeployError(f'invalid {label}')
    return result


def resource_id(value: object, label: str) -> str:
    result = text(value, label)
    if re.fullmatch('[a-z0-9]{20}', result) is None:
        raise DeployError(f'invalid {label}')
    return result


@dataclass(frozen=True)
class Settings:
    cloud_id: str
    folder_id: str
    service_account_id: str
    function_id: str
    runtime_account_id: str
    bucket: str
    archive: Path
    secret_id: str
    secret_version_id: str

    def secret(self) -> dict[str, str]:
        return {'id': self.secret_id, 'versionId': self.secret_version_id,
                'key': 'z_token', 'environmentVariable': 'ZONT_TOKEN'}


def settings_from_environment(environment: Mapping[str, str]) -> Settings:
    """Validate every repository variable and fixed setting before requesting OIDC."""
    shared = {'CLOUD_ID': 'b1gbga988v36vq19lmb7', 'FOLDER_ID': 'b1gdgrp5bnth11phova8',
              'FUNCTION_SA_ID': 'ajelpbq7as5bpe499962', 'BUCKET': 'autorepeater-deploy',
              'GITHUB_OIDC_AUDIENCE': 'https://github.com/gorpsys'}
    fixed = {'FUNCTION_RUNTIME': 'python312', 'FUNCTION_ENTRYPOINT': 'handler.handler',
             'FUNCTION_MEMORY': '256MB', 'FUNCTION_TIMEOUT': '420s',
             'FUNCTION_CONCURRENCY': '1', 'LOCKBOX_KEY': 'z_token',
             'LOCKBOX_ENVIRONMENT_VARIABLE': 'ZONT_TOKEN'}
    if any(environment.get(key) != value for key, value in {**shared, **fixed}.items()):
        raise DeployError('missing or unexpected deployment settings')
    ids = {key: resource_id(environment.get(key), key) for key in (
        'SA_ID', 'FUNCTION_ID', 'LOCKBOX_SECRET_ID', 'LOCKBOX_VERSION_ID')}
    # These belong to the sample application, never to this deployment.
    if (ids['SA_ID'] == 'ajela8pd8l3bjeupa04s'
            or ids['FUNCTION_ID'] == 'd4e6d0l53hkivgrmfano'
            or ids['LOCKBOX_SECRET_ID'] == 'e6qc2ghhip6925lllhm7'
            or ids['SA_ID'] == shared['FUNCTION_SA_ID']):
        raise DeployError('deployment requires separate application resources')
    try:
        load_controller_config(reader=lambda _path: {
            'contact': environment.get('ZONT_CONTACT'), 'http_timeout_seconds': 15})
    except ConfigurationError:
        raise DeployError('invalid ZONT_CONTACT') from None
    archive = Path(text(environment.get('ZIP_PATH'), 'ZIP_PATH')).absolute()
    return Settings(shared['CLOUD_ID'], shared['FOLDER_ID'], ids['SA_ID'],
                    ids['FUNCTION_ID'], shared['FUNCTION_SA_ID'], shared['BUCKET'], archive,
                    ids['LOCKBOX_SECRET_ID'], ids['LOCKBOX_VERSION_ID'])


def exchange_token(environment: Mapping[str, str], service_account_id: str) -> str:
    """GitHub federation AUD and Yandex deployment-SA audience are separate values."""
    url = urlsplit(text(environment.get('ACTIONS_ID_TOKEN_REQUEST_URL'), 'OIDC request URL'))
    if (url.scheme != 'https' or not url.hostname
            or not url.hostname.endswith('.actions.githubusercontent.com')
            or url.username or url.password or url.port not in (None, 443) or url.fragment):
        raise DeployError('untrusted OIDC request URL')
    audience = environment.get('GITHUB_OIDC_AUDIENCE')
    if audience != 'https://github.com/gorpsys':
        raise DeployError('invalid GitHub OIDC audience')
    iam_audience = resource_id(service_account_id, 'deployment service account')
    query = [(key, value) for key, value in parse_qsl(url.query) if key != 'audience']
    request_url = urlunsplit(url._replace(query=urlencode([*query, ('audience', audience)])))
    request_token = text(environment.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN'), 'OIDC request token')
    oidc = text(record(request_json(Request(request_url, headers={
        'Authorization': f'Bearer {request_token}'}), stage='GitHub OIDC')).get('value'), 'OIDC token')
    body = urlencode({'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
                      'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
                      'audience': iam_audience, 'subject_token': oidc,
                      'subject_token_type': 'urn:ietf:params:oauth:token-type:id_token'}).encode()
    response = record(request_json(Request('https://auth.yandex.cloud/oauth/token', data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}), stage='Yandex IAM exchange'))
    expires = response.get('expires_in')
    if response.get('token_type') != 'Bearer' or type(expires) is not int or expires <= 0:
        raise DeployError('invalid IAM token response')
    # Tokens never enter workflow outputs, masking commands, logs, argv or config.
    return text(response.get('access_token'), 'IAM token')


class YandexCLI:
    """Finite commands, private output and a token-free explicit temporary profile."""

    def __init__(self, settings: Settings, token: str, temporary_root: Path) -> None:
        self.settings = settings
        self.token = text(token, 'IAM token')
        self.temporary_root = temporary_root

    def call(self, arguments: Sequence[str], label: str, *,
             timeout: int = 30) -> dict[str, object] | list[object]:
        # Do not inherit alternate yc credentials/profile overrides or GitHub/ZONT tokens.
        environment = {key: value for key, value in os.environ.items() if key in (
            'PATH', 'HOME', 'SYSTEMROOT', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL',
            'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY')}
        environment['YC_IAM_TOKEN'] = self.token
        try:
            with TemporaryDirectory(prefix='yc-deploy-', dir=self.temporary_root) as directory:
                config = Path(directory) / 'config.yaml'
                config.write_text(json.dumps({'current': 'ci', 'profiles': {'ci': {
                    'endpoint': 'api.cloud.yandex.net:443', 'cloud-id': self.settings.cloud_id,
                    'folder-id': self.settings.folder_id}}}), encoding='utf-8')
                config.chmod(0o600)
                command = ['yc', *arguments, '--config', str(config), '--profile', 'ci',
                           '--cloud-id', self.settings.cloud_id, '--folder-id', self.settings.folder_id,
                           '--endpoint', 'api.cloud.yandex.net:443', '--format', 'json-rest',
                           '--timeout', f'{timeout}s', '--retry', '0',
                           '--no-user-output', '--no-browser']
                result = subprocess.run(command, env=environment, capture_output=True,
                                        timeout=timeout + 15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DeployError(f'cloud command unavailable or timed out: {label}') from None
        if result.returncode:
            raise DeployError(f'cloud command failed: {label}; exit={result.returncode}; '
                              f'reason={cli_failure_reason(result.stderr)}')
        if len(result.stdout) > MAX_RESPONSE_BYTES:
            raise DeployError(f'cloud response exceeds size limit: {label}')
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, (dict, list)):
                raise ValueError
            return data
        except (ValueError, UnicodeError):
            raise DeployError(f'invalid cloud JSON: {label}') from None


def cli_failure_reason(stderr: bytes) -> str:
    """Classify private CLI output; never forward provider text or arguments."""
    patterns = (
        (b'code = PermissionDenied', 'permission_denied'), (b'AccessDenied', 'permission_denied'),
        (b'code = Unauthenticated', 'unauthenticated'), (b'InvalidToken', 'unauthenticated'),
        (b'code = NotFound', 'not_found'), (b'NoSuchBucket', 'not_found'),
        (b'code = Unavailable', 'unavailable'), (b'code = DeadlineExceeded', 'deadline_exceeded'),
        (b'context deadline exceeded', 'deadline_exceeded'),
        (b'code = InvalidArgument', 'invalid_argument'),
        (b'code = FailedPrecondition', 'failed_precondition'),
        (b'code = ResourceExhausted', 'resource_exhausted'),
        (b'code = Internal', 'internal'), (b'code = Unimplemented', 'unimplemented'),
        (b'unknown flag:', 'unsupported_flag'),
        (b'no such file or directory', 'local_file_unavailable'),
        (b'Failed to get credentials', 'credentials_missing'),
    )
    return next((reason for pattern, reason in patterns if pattern in stderr), 'unknown')


def package_digest(path: Path, sha: str) -> str:
    try:
        verify_archive(path, expected_sha=sha, require_clean=True)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (ArchiveError, OSError):
        raise DeployError('invalid cloud archive or provenance') from None


def known_fields(value: object, allowed: Iterable[str]) -> dict[str, object]:
    data = record(value)
    if data.keys() - set(allowed):
        raise DeployError('unsupported cloud settings')
    return data


def environment_variables(current: dict[str, object]) -> dict[str, str]:
    values = record(current.get('environment', {}))
    if any(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', key) is None
           or not isinstance(value, str) or any(char in value for char in '\x00\r\n')
           for key, value in values.items()) or 'ZONT_TOKEN' in values:
        raise DeployError('invalid function environment; token requires Lockbox')
    return values


def read_secret_bindings(current: dict[str, object]) -> list[dict[str, str]]:
    values = current.get('secrets', [])
    if not isinstance(values, list):
        raise DeployError('invalid secret references')
    result, seen = [], set()
    for value in values:
        fields = ('id', 'versionId', 'key', 'environmentVariable')
        data = known_fields(value, fields)
        binding = {key: identifier(data.get(key), 'secret reference') for key in fields}
        if binding['environmentVariable'] in seen:
            raise DeployError('duplicate secret environment variable')
        seen.add(binding['environmentVariable'])
        result.append(binding)
    return result


def secret_bindings(settings: Settings, current: dict[str, object]) -> list[dict[str, str]]:
    return [item for item in read_secret_bindings(current)
            if item['environmentVariable'] != 'ZONT_TOKEN'] + [settings.secret()]


def logging_arguments(settings: Settings, value: object) -> list[str]:
    options = known_fields(value, ('disabled', 'folderId', 'logGroupId', 'minLevel'))
    # The approved destination is this shared folder's default log group.
    if options.get('disabled', False) is not False or options.get('logGroupId') or (
            options.get('folderId') != settings.folder_id):
        raise DeployError('cloud logs must use the approved folder')
    args = ['--log-folder-id', settings.folder_id]
    level = options.get('minLevel', 'LEVEL_UNSPECIFIED')
    if level != 'LEVEL_UNSPECIFIED':
        if level not in ('TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'):
            raise DeployError('invalid cloud log level')
        args.extend(('--min-log-level', level.lower()))
    return args


def preserved_arguments(settings: Settings, current: dict[str, object]) -> list[str]:
    known_fields(current, ('id', 'functionId', 'createdAt', 'runtime', 'entrypoint',
        'resources', 'executionTimeout', 'serviceAccountId', 'status', 'description',
        'tags', 'environment', 'secrets', 'concurrency', 'tmpfsSize', 'connectivity',
        'namedServiceAccounts', 'metadataOptions', 'logOptions', 'packageSize', 'imageSize',
        'mounts', 'storageMounts', 'asyncInvocationConfig'))
    known_fields(current.get('resources', {}), ('memory',))
    if any(current.get(key) for key in ('mounts', 'storageMounts', 'asyncInvocationConfig')):
        raise DeployError('mounts or async configuration require explicit deployment support')
    args = []
    if 'tmpfsSize' in current:
        value = text(current['tmpfsSize'], 'tmpfs size')
        if re.fullmatch('[0-9]+', value) is None:
            raise DeployError('invalid tmpfs size')
        args.extend(('--tmpfs-size', value))
    connectivity = known_fields(current.get('connectivity', {}), ('networkId',))
    if connectivity.get('networkId'):
        args.extend(('--network-id', identifier(connectivity['networkId'], 'network ID')))
    for alias, account in record(current.get('namedServiceAccounts', {})).items():
        args.extend(('--add-service-account', f'alias={identifier(alias, "account alias")},'
                     f'id={identifier(account, "named service account")}'))
    options = known_fields(current.get('metadataOptions', {}),
                           ('awsV1HttpEndpoint', 'gceHttpEndpoint'))
    if options:
        if any(value not in ('ENABLED', 'DISABLED') for value in options.values()):
            raise DeployError('invalid metadata endpoint options')
        fields = {'awsV1HttpEndpoint': 'aws-v1-http-endpoint',
                  'gceHttpEndpoint': 'gce-http-endpoint'}
        args.extend(('--metadata-options', ','.join(
            f'{fields[key]}={value.lower()}' for key, value in options.items())))
    args.extend(logging_arguments(settings, current.get('logOptions', {})))
    return args


def description(sha: str, digest: str, object_key: str) -> str:
    return f'zontcontrol source_sha={sha}; package_sha256={digest}; object={object_key}'


def version_arguments(settings: Settings, current: dict[str, object], object_key: str,
                      digest: str, sha: str) -> list[str]:
    if current:
        if current.get('functionId') != settings.function_id or current.get('status') != 'ACTIVE':
            raise DeployError('current version target or status mismatch')
        resource_id(current.get('id'), 'current version ID')
    values = environment_variables(current)
    bindings = secret_bindings(settings, current)
    if set(values) & {binding['environmentVariable'] for binding in bindings}:
        raise DeployError('environment and secret bindings overlap')
    preserved = preserved_arguments(settings, current) if current else [
        '--log-folder-id', settings.folder_id]
    args = ['serverless', 'function', 'version', 'create', '--function-id', settings.function_id,
            '--runtime', 'python312', '--entrypoint', 'handler.handler', '--memory', '256MB',
            '--execution-timeout', '420s', '--concurrency', '1',
            '--service-account-id', settings.runtime_account_id,
            '--package-bucket-name', settings.bucket, '--package-object-name', object_key,
            '--package-sha256', digest, '--description', description(sha, digest, object_key)]
    if values:
        output = io.StringIO()
        csv.writer(output, lineterminator='\n').writerow([f'{key}={value}' for key, value in values.items()])
        args.extend(('--environment', output.getvalue().removesuffix('\n')))
    for binding in bindings:
        args.extend(('--secret', ','.join(f'{key}={binding[field]}' for key, field in (
            ('environment-variable', 'environmentVariable'), ('id', 'id'),
            ('version-id', 'versionId'), ('key', 'key')))))
    return args + preserved


def verify_version(settings: Settings, before: dict[str, object], after: dict[str, object], *,
                   sha: str, digest: str, object_key: str) -> None:
    resource_id(after.get('id'), 'new version ID')
    expected = {'functionId': settings.function_id, 'status': 'ACTIVE', 'runtime': 'python312',
                'entrypoint': 'handler.handler', 'executionTimeout': '420s', 'concurrency': '1',
                'serviceAccountId': settings.runtime_account_id,
                'description': description(sha, digest, object_key),
                'resources': {'memory': str(256 * 1024 ** 2)}}
    if after.get('id') == before.get('id') or any(after.get(k) != v for k, v in expected.items()):
        raise DeployError('published version identity or execution settings mismatch')
    if environment_variables(after) != environment_variables(before) or sorted(
            read_secret_bindings(after), key=lambda b: b['environmentVariable']) != sorted(
            secret_bindings(settings, before), key=lambda b: b['environmentVariable']):
        raise DeployError('published version environment or secret bindings mismatch')
    baseline = before or {'logOptions': {'folderId': settings.folder_id}}
    # Validate the entire readback as well, rejecting newly introduced unknown settings.
    preserved_arguments(settings, after)
    for key, default in (('tmpfsSize', '0'), ('connectivity', {}), ('namedServiceAccounts', {}),
                         ('metadataOptions', {})):
        if before.get(key, default) != after.get(key, default):
            raise DeployError('published version operational settings mismatch')
    if logging_arguments(settings, baseline['logOptions']) != logging_arguments(
            settings, after['logOptions']):
        raise DeployError('published version logging mismatch')


def run_identity(run_id: object, attempt: object) -> None:
    if any(not isinstance(v, str) or re.fullmatch('[1-9][0-9]*', v) is None
           for v in (run_id, attempt)):
        raise DeployError('invalid deployment run identity')


def publish(settings: Settings, yc: YandexCLI, sha: str, run_id: object, attempt: object) -> str:
    """Only version publication and a unique ZIP upload mutate cloud state."""
    sha = commit_sha(sha)
    run_identity(run_id, attempt)
    digest = package_digest(settings.archive, sha)
    function = record(yc.call(['serverless', 'function', 'get', '--id', settings.function_id],
                              'read function'))
    if function.get('id') != settings.function_id or function.get('folderId') != settings.folder_id:
        raise DeployError('function ID or folder mismatch')
    versions = yc.call(['serverless', 'function', 'version', 'list', '--function-id',
                       settings.function_id, '--limit', '1'], 'list versions')
    if not isinstance(versions, list):
        raise DeployError('invalid version list')
    for version in versions:
        if record(version).get('functionId') != settings.function_id:
            raise DeployError('listed version belongs to another function')
        resource_id(version.get('id'), 'listed version ID')
    get_latest = ['serverless', 'function', 'version', 'get-by-tag',
                  '--function-id', settings.function_id, '--tag', '$latest']
    before = record(yc.call(get_latest, 'read current version')) if versions else {}
    if versions and not before:
        raise DeployError('nonempty version list requires a valid latest version')
    key = f'functions/zontcontrol/{sha}/{run_id}-{attempt}.zip'
    arguments = version_arguments(settings, before, key, digest, sha)
    if package_digest(settings.archive, sha) != digest:
        raise DeployError('cloud archive changed before upload')
    print(f'Publishing {sha}; package sha256={digest}; object={key}', flush=True)
    try:
        yc.call(['storage', 's3api', 'put-object', '--body', '@' + str(settings.archive),
                 '--bucket', settings.bucket, '--key', key], 'upload package', timeout=120)
    except DeployError as upload_error:
        # Read the real object once. A mere HEAD cannot prove its full byte hash.
        try:
            yc.call(['storage', 's3api', 'head-object', '--bucket', settings.bucket,
                     '--key', key], 'inspect uncertain upload')
            inspection = 'HEAD succeeded; byte hash remains unproven'
        except DeployError as head_error:
            inspection = str(head_error)
        raise DeployError('upload outcome cannot be proven; write was not retried; '
                          f'upload: {upload_error}; inspection: {inspection}') from None
    created = None
    try:
        created = record(yc.call(arguments, 'create function version', timeout=900))
    except DeployError:
        # An uncertain write is never repeated; unique provenance can prove its result.
        pass
    after = record(yc.call(get_latest, 'verify latest version'))
    verify_version(settings, before, after, sha=sha, digest=digest, object_key=key)
    if created is not None and (created.get('functionId') != settings.function_id
                                or created.get('id') != after['id']):
        raise DeployError('created version differs from the configured function or latest version')
    print(f'Deployed ACTIVE version {after["id"]} for master {sha}', flush=True)
    return after['id']


def main() -> int:
    try:
        environment = dict(os.environ)
        if environment.get('GITHUB_REPOSITORY') != REPOSITORY or (
                environment.get('GITHUB_REF') != 'refs/heads/master'):
            raise DeployError('deployment requires repository master')
        if environment.get('GITHUB_EVENT_NAME') not in ('workflow_run', 'workflow_dispatch'):
            raise DeployError('unsupported deployment event')
        settings = settings_from_environment(environment)
        sha = commit_sha(environment.get('DEPLOY_SHA'))
        commit_sha(environment.get('GITHUB_SHA'))
        run_id = environment.get('GITHUB_RUN_ID')
        attempt = environment.get('GITHUB_RUN_ATTEMPT')
        run_identity(run_id, attempt)
        temporary_root = Path(text(environment.get('RUNNER_TEMP'), 'RUNNER_TEMP'))
        if not temporary_root.is_dir() or not environment.get('GH_TOKEN'):
            raise DeployError('invalid runner configuration')
        package_digest(settings.archive, sha)
        if authorize(environment) != sha:
            raise DeployError('master changed before credentials')
        token = exchange_token(environment, settings.service_account_id)
        publish(settings, YandexCLI(settings, token, temporary_root), sha, run_id, attempt)
    except DeployError as error:
        print(f'Deployment stopped: {error}', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print('Deployment configuration or response invalid', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

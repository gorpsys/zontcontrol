"""Fake-only OIDC, CLI and publication contract, without cloud or ZONT requests."""

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts import build_yandex_archive as builder
from scripts import yandex_deploy as deploy
from scripts import deploy_support as support
from scripts.deploy_support import DeployError


SHA = 'a' * 40
ROOT = Path(__file__).resolve().parents[1]
FUNCTION = 'd4' + 'n' * 18
DEPLOY_SA = 'aje' + 'n' * 17
SECRET = 'e6q' + 'n' * 17
SECRET_VERSION = 'e6q' + 'v' * 17
IAM = 'synthetic-private-iam'


def environment(path):
    return {'CLOUD_ID': 'b1gbga988v36vq19lmb7', 'FOLDER_ID': 'b1gdgrp5bnth11phova8',
            'SA_ID': DEPLOY_SA, 'FUNCTION_ID': FUNCTION,
            'FUNCTION_SA_ID': 'ajelpbq7as5bpe499962', 'BUCKET': 'autorepeater-deploy',
            'LOCKBOX_SECRET_ID': SECRET, 'LOCKBOX_VERSION_ID': SECRET_VERSION,
            'GITHUB_OIDC_AUDIENCE': 'https://github.com/gorpsys',
            'ZONT_CONTACT': 'deploy@example.invalid', 'FUNCTION_RUNTIME': 'python312',
            'FUNCTION_ENTRYPOINT': 'handler.handler', 'FUNCTION_MEMORY': '256MB',
            'FUNCTION_TIMEOUT': '420s', 'FUNCTION_CONCURRENCY': '1',
            'LOCKBOX_KEY': 'z_token', 'LOCKBOX_ENVIRONMENT_VARIABLE': 'ZONT_TOKEN',
            'ZIP_PATH': str(path), 'GITHUB_REPOSITORY': 'gorpsys/zontcontrol',
            'GITHUB_REF': 'refs/heads/master', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
            'GITHUB_SHA': SHA, 'DEPLOY_SHA': SHA, 'GITHUB_RUN_ID': '12',
            'GITHUB_RUN_ATTEMPT': '2', 'RUNNER_TEMP': str(path.parent),
            'GH_TOKEN': 'synthetic-private-github',
            'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://runner.actions.githubusercontent.com/token?x=1',
            'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic-private-request'}


def binding():
    return {'id': SECRET, 'versionId': SECRET_VERSION, 'key': 'z_token',
            'environmentVariable': 'ZONT_TOKEN'}


def csv_options(value):
    return dict(field.split('=', 1) for field in next(csv.reader([value])))


def requested_bindings(args):
    names = {'id': 'id', 'version-id': 'versionId', 'key': 'key',
             'environment-variable': 'environmentVariable'}
    return [{names[key]: value for key, value in csv_options(args[index + 1]).items()}
            for index, flag in enumerate(args) if flag == '--secret']


def current():
    return {'id': 'd4' + 'o' * 18, 'functionId': FUNCTION, 'status': 'ACTIVE',
            'runtime': 'python312', 'entrypoint': 'handler.handler',
            'resources': {'memory': '268435456'}, 'executionTimeout': '420s',
            'serviceAccountId': 'ajelpbq7as5bpe499962', 'concurrency': '1',
            'environment': {'KEEP': 'a,b=c with spaces'},
            'secrets': [{'id': 'e6q' + 'o' * 17, 'versionId': 'e6q' + 'p' * 17,
                         'key': 'keep', 'environmentVariable': 'KEEP_SECRET'}],
            'logOptions': {'folderId': 'b1gdgrp5bnth11phova8', 'minLevel': 'INFO'},
            'tmpfsSize': '536870912', 'connectivity': {'networkId': 'en' + 'n' * 18},
            'namedServiceAccounts': {'other': 'aje' + 'o' * 17},
            'metadataOptions': {'awsV1HttpEndpoint': 'DISABLED'}}


class FakeYC:
    def __init__(self, before=None):
        self.before = deepcopy(before)
        self.calls = []
        self.after = None
        self.fail_list = False
        self.fail_upload = False
        self.fail_create = False
        self.after_change = lambda after: None
        self.created_change = lambda created: None
        self.function_id = FUNCTION

    def call(self, args, label, **kwargs):
        self.calls.append(args)
        if args[:3] == ['serverless', 'function', 'get']:
            return {'id': self.function_id, 'folderId': 'b1gdgrp5bnth11phova8'}
        if 'list' in args:
            if self.fail_list:
                raise DeployError('synthetic list failure')
            return [] if self.before is None else [self.before]
        if 'get-by-tag' in args:
            return deepcopy(self.after if self.after is not None else self.before)
        if args[:3] == ['storage', 's3api', 'put-object']:
            if self.fail_upload:
                raise DeployError('synthetic upload outcome unknown')
            return {}
        if 'create' in args:
            self.after = deepcopy(self.before or {})
            self.after.update({'id': 'd4' + 'v' * 18, 'functionId': FUNCTION, 'status': 'ACTIVE',
                'runtime': 'python312', 'entrypoint': 'handler.handler',
                'resources': {'memory': '268435456'}, 'executionTimeout': '420s',
                'serviceAccountId': 'ajelpbq7as5bpe499962', 'concurrency': '1',
                'description': args[args.index('--description') + 1]})
            if self.before is None:
                self.after.update(environment={}, secrets=[],
                                  logOptions={'folderId': 'b1gdgrp5bnth11phova8'})
            self.after['secrets'] = [s for s in self.after['secrets']
                                     if s['environmentVariable'] != 'ZONT_TOKEN'] + [binding()]
            self.after_change(self.after)
            result = {'id': self.after['id'], 'functionId': FUNCTION}
            self.created_change(result)
            if self.fail_create:
                raise DeployError('synthetic create outcome unknown')
            return result
        raise AssertionError(args)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'function.zip'
        with patch.object(builder, 'source_identity', return_value=(SHA, False)):
            builder.build_archive(ROOT, self.path, contact='deploy@example.invalid')
        self.env = environment(self.path)
        self.settings = deploy.settings_from_environment(self.env)
        self.network = patch.object(deploy, 'request_json', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def publish(self, yc):
        with redirect_stdout(io.StringIO()):
            return deploy.publish(self.settings, yc, SHA, '12', '2')

    def test_first_version_uses_id_and_reference_plain_file_body(self):
        yc = FakeYC()
        self.assertEqual(self.publish(yc), 'd4' + 'v' * 18)
        create = next(args for args in yc.calls if 'create' in args)
        upload = next(args for args in yc.calls if 'put-object' in args)
        self.assertEqual(upload[upload.index('--body') + 1], str(self.path))
        self.assertIn(f'functions/zontcontrol/{SHA}/12-2.zip', upload)
        for flag, value in [('--function-id', FUNCTION), ('--runtime', 'python312'),
                            ('--entrypoint', 'handler.handler'), ('--memory', '256MB'),
                            ('--execution-timeout', '420s'), ('--concurrency', '1'),
                            ('--service-account-id', self.env['FUNCTION_SA_ID']),
                            ('--log-folder-id', self.env['FOLDER_ID'])]:
            self.assertEqual(create[create.index(flag) + 1], value)
        self.assertEqual(requested_bindings(create), [binding()])
        self.assertNotIn('--environment', create)
        self.assertNotIn('--function-name', create)
        self.assertNotIn('--async-max-retries', create)
        self.assertEqual(sum('get-by-tag' in args for args in yc.calls), 1)

    def test_existing_settings_env_and_other_secrets_survive(self):
        yc = FakeYC(current())
        self.publish(yc)
        args = next(a for a in yc.calls if 'create' in a)
        self.assertEqual(csv_options(args[args.index('--environment') + 1]), current()['environment'])
        self.assertEqual(requested_bindings(args), current()['secrets'] + [binding()])
        for flag, value in [('--tmpfs-size', '536870912'), ('--network-id', 'en' + 'n' * 18),
                            ('--min-log-level', 'info')]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(csv_options(args[args.index('--add-service-account') + 1]),
                         {'alias': 'other', 'id': 'aje' + 'o' * 17})
        self.assertEqual(csv_options(args[args.index('--metadata-options') + 1]),
                         {'aws-v1-http-endpoint': 'disabled'})

    def test_pinned_token_reference_is_replaced_and_other_bindings_remain(self):
        before = current()
        before['secrets'].append(dict(binding(), versionId='e6q' + 'o' * 17))
        yc = FakeYC(before)
        self.publish(yc)
        args = next(a for a in yc.calls if 'create' in a)
        self.assertEqual(requested_bindings(args), current()['secrets'] + [binding()])

    def test_read_error_wrong_target_and_unsupported_fields_fail_before_upload(self):
        cases = []
        bad = FakeYC(); bad.fail_list = True; cases.append(bad)
        bad = FakeYC(); bad.function_id = 'd4' + 'x' * 18; cases.append(bad)
        for update in ({'functionId': 'd4' + 'x' * 18}, {'unknownOption': True},
                       {'resources': {'memory': '1', 'unknown': '1'}},
                       {'logOptions': {'folderId': 'elsewhere'}},
                       {'connectivity': {'networkId': 'network', 'unknown': True}},
                       {'asyncInvocationConfig': {'retriesCount': '1'}},
                       {'mounts': [{'bucket': 'other'}]},
                       {'secrets': [dict(binding(), extra='unknown')]},
                       {'logOptions': {}},
                       {'environment': {'KEEP_SECRET': 'conflicts-with-binding'}},
                       {'environment': {'ZONT_TOKEN': 'must-use-lockbox'}}):
            before = current(); before.update(update); cases.append(FakeYC(before))
        for yc in cases:
            with self.subTest(before=yc.before), self.assertRaises(DeployError):
                self.publish(yc)
            self.assertFalse(any('put-object' in args for args in yc.calls))

    def test_nonempty_list_does_not_allow_an_empty_or_missing_latest(self):
        for latest in ({}, {'id': current()['id']}, None):
            yc = FakeYC(current())
            original = yc.call
            def call(args, label, **kwargs):
                if 'get-by-tag' in args:
                    return latest
                return original(args, label, **kwargs)
            yc.call = call
            with self.subTest(latest=latest), self.assertRaises(DeployError):
                self.publish(yc)
            self.assertFalse(any('put-object' in args for args in yc.calls))

    def test_malformed_latest_settings_are_reported_without_provider_text(self):
        for change in ({'logOptions': None}, {'environment': {'bad,key': IAM}},
                       {'secrets': [binding(), binding()]}, {'tmpfsSize': 'unsafe'},
                       {'metadataOptions': {'unknown': IAM}}):
            before = current(); before.update(change)
            yc = FakeYC(before)
            with self.subTest(change=change), self.assertRaises(DeployError) as error:
                self.publish(yc)
            self.assertNotIn(IAM, str(error.exception))
            self.assertFalse(any('put-object' in args for args in yc.calls))

    def test_invalid_list_shapes_are_never_interpreted_as_empty(self):
        for versions in ({}, None, {'versions': []}, [{'id': current()['id']}],
                         [dict(current(), functionId='d4' + 'x' * 18)]):
            yc = FakeYC()
            original = yc.call
            def call(args, label, **kwargs):
                return versions if 'list' in args else original(args, label, **kwargs)
            yc.call = call
            with self.subTest(versions=versions), self.assertRaises(DeployError):
                self.publish(yc)
            self.assertFalse(any('put-object' in args for args in yc.calls))

    def test_readback_must_verify_all_identity_settings_and_secret_bindings(self):
        for key, value in [('functionId', 'd4' + 'x' * 18), ('id', 'd4' + 'o' * 18),
                           ('status', 'CREATING'), ('runtime', 'python311'),
                           ('entrypoint', 'wrong.handler'), ('executionTimeout', '60s'),
                           ('resources', {'memory': '1'}), ('concurrency', '0'),
                           ('serviceAccountId', DEPLOY_SA), ('logOptions', {}),
                           ('secrets', []), ('environment', {}), ('description', 'other'),
                           ('tmpfsSize', '0'), ('namedServiceAccounts', {}),
                           ('metadataOptions', {}), ('connectivity', {})]:
            yc = FakeYC(current()); yc.after_change = lambda after, k=key, v=value: after.update({k: v})
            with self.subTest(key=key), self.assertRaises(DeployError):
                self.publish(yc)

    def test_created_response_must_match_function_and_latest_version(self):
        for key, value in [('functionId', 'd4' + 'x' * 18), ('id', 'd4' + 'x' * 18)]:
            yc = FakeYC(current()); yc.created_change = lambda c, k=key, v=value: c.update({k: v})
            with self.subTest(key=key), self.assertRaises(DeployError):
                self.publish(yc)

    def test_unknown_create_reads_actual_result_without_retrying_write(self):
        yc = FakeYC(current()); yc.fail_create = True
        self.assertEqual(self.publish(yc), 'd4' + 'v' * 18)
        self.assertEqual(sum('create' in a for a in yc.calls), 1)
        yc = FakeYC(current()); yc.fail_create = True
        yc.after_change = lambda a: a.update(description='unproven')
        with self.assertRaises(DeployError):
            self.publish(yc)
        self.assertEqual(sum('create' in a for a in yc.calls), 1)

    def test_unknown_upload_stops_without_retry_or_version_creation(self):
        yc = FakeYC(); yc.fail_upload = True
        with self.assertRaises(DeployError):
            self.publish(yc)
        self.assertEqual(sum('put-object' in a for a in yc.calls), 1)
        self.assertFalse(any('head-object' in a for a in yc.calls))
        self.assertFalse(any('create' in a for a in yc.calls))

    def test_unknown_create_without_latest_proof_fails_without_repeating(self):
        yc = FakeYC(current()); yc.fail_create = True
        original = yc.call
        def call(args, label, **kwargs):
            if 'get-by-tag' in args and yc.after is not None:
                raise DeployError('read outcome unavailable')
            return original(args, label, **kwargs)
        yc.call = call
        with self.assertRaises(DeployError):
            self.publish(yc)
        self.assertEqual(sum('create' in args for args in yc.calls), 1)

    def test_dirty_or_wrong_sha_archive_is_rejected_before_cloud(self):
        for sha, dirty in [('b' * 40, False), (SHA, True)]:
            with patch.object(builder, 'source_identity', return_value=(sha, dirty)):
                builder.build_archive(ROOT, self.path, contact='deploy@example.invalid')
            yc = FakeYC()
            with self.assertRaises(DeployError):
                self.publish(yc)
            self.assertEqual(yc.calls, [])

    def test_empty_or_invalid_parameters_rejected_before_oidc(self):
        for key in self.env:
            if key.startswith('ACTIONS_') or key in ('GH_TOKEN', 'GITHUB_EVENT_PATH'):
                continue
            env = dict(self.env, **{key: ''})
            with self.subTest(key=key), patch.dict(os.environ, env, clear=True), \
                    patch.object(deploy, 'exchange_token') as exchange, \
                    patch.object(deploy, 'authorize', return_value=SHA), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(deploy.main(), 1)
                exchange.assert_not_called()
        for key, value in [('FUNCTION_ID', 'd4e6d0l53hkivgrmfano'),
                           ('SA_ID', 'ajela8pd8l3bjeupa04s'), ('FUNCTION_TIMEOUT', '60s'),
                           ('GITHUB_OIDC_AUDIENCE', DEPLOY_SA), ('LOCKBOX_KEY', 't_token')]:
            with self.subTest(key=key), self.assertRaises(DeployError):
                deploy.settings_from_environment(dict(self.env, **{key: value}))

    def test_master_race_denies_credentials_even_when_workflow_gate_previously_passed(self):
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(deploy, 'authorize', side_effect=DeployError('stale master')), \
                patch.object(deploy, 'exchange_token') as exchange, redirect_stderr(io.StringIO()):
            self.assertEqual(deploy.main(), 1)
            exchange.assert_not_called()

    def test_main_success_passes_gate_before_exchange_and_never_prints_credentials(self):
        order = []
        def authorize(env):
            order.append('gate')
            self.assertEqual(env['DEPLOY_SHA'], SHA)
            return SHA
        def exchange(env, account):
            order.append('exchange')
            self.assertEqual(account, DEPLOY_SA)
            return IAM
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(deploy, 'authorize', side_effect=authorize), \
                patch.object(deploy, 'exchange_token', side_effect=exchange), \
                patch.object(deploy, 'YandexCLI', return_value=FakeYC()), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(deploy.main(), 0)
        self.assertEqual(order, ['gate', 'exchange'])
        for value in (IAM, self.env['GH_TOKEN'], self.env['ACTIONS_ID_TOKEN_REQUEST_TOKEN']):
            self.assertNotIn(value, output.getvalue() + errors.getvalue())

    def test_oidc_refuses_untrusted_urls_and_malformed_token_responses(self):
        for url in ('http://runner.actions.githubusercontent.com/token',
                    'https://evil.invalid/token',
                    'https://runner.actions.githubusercontent.com.evil.invalid/token',
                    'https://user@runner.actions.githubusercontent.com/token',
                    'https://runner.actions.githubusercontent.com:444/token'):
            with self.subTest(url=url), patch.object(deploy, 'request_json') as request:
                with self.assertRaises(DeployError):
                    deploy.exchange_token(dict(self.env, ACTIONS_ID_TOKEN_REQUEST_URL=url), DEPLOY_SA)
                request.assert_not_called()
        for response in ({}, {'token_type': 'Bearer', 'expires_in': True, 'access_token': IAM},
                         {'token_type': 'Bearer', 'expires_in': 0, 'access_token': IAM}):
            with self.subTest(response=response), patch.object(deploy, 'request_json',
                    side_effect=[{'value': 'synthetic-oidc'}, response]):
                with self.assertRaises(DeployError) as error:
                    deploy.exchange_token(self.env, DEPLOY_SA)
                self.assertNotIn(IAM, str(error.exception))

    def test_oidc_federation_audience_and_iam_sa_audience_are_distinct(self):
        with patch.object(deploy, 'request_json', side_effect=[{'value': 'synthetic-oidc'},
                {'access_token': IAM, 'token_type': 'Bearer', 'expires_in': 3600}]) as request, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(deploy.exchange_token(self.env, DEPLOY_SA), IAM)
        first, second = [call.args[0] for call in request.call_args_list]
        self.assertEqual(parse_qs(urlsplit(first.full_url).query)['audience'],
                         ['https://github.com/gorpsys'])
        self.assertEqual(parse_qs(second.data.decode())['audience'], [DEPLOY_SA])
        for credential in (IAM, 'synthetic-oidc', self.env['ACTIONS_ID_TOKEN_REQUEST_TOKEN']):
            self.assertNotIn(credential, output.getvalue())

    def test_private_cli_env_config_timeout_and_cleanup_on_all_paths(self):
        for outcome in ('success', 'failure', 'timeout', 'invalid', 'oserror'):
            paths = []
            def execute(args, **kwargs):
                self.assertNotIn(IAM, repr(args))
                self.assertNotIn(IAM, repr(kwargs.get('input')))
                self.assertEqual(kwargs['env']['YC_IAM_TOKEN'], IAM)
                self.assertEqual(kwargs['env']['HOME'], '/synthetic/home')
                self.assertNotIn('YC_TOKEN', kwargs['env'])
                self.assertEqual(kwargs['env']['RUNNER_REFERENCE_VALUE'], 'inherited')
                self.assertNotIn('--cloud-id', args)
                self.assertNotIn('--endpoint', args)
                self.assertEqual(args[args.index('--retry') + 1], '0')
                self.assertEqual(args[args.index('--format') + 1], 'json-rest')
                self.assertGreater(kwargs['timeout'], 0)
                config = Path(args[args.index('--config') + 1]); paths.append(config)
                self.assertEqual(json.loads(config.read_text()), {
                    'current': 'ci', 'profiles': {'ci': {
                        'cloud-id': self.settings.cloud_id,
                        'folder-id': self.settings.folder_id,
                        'endpoint': 'api.cloud.yandex.net:443'}}})
                self.assertNotIn(IAM, config.read_text())
                if outcome == 'timeout':
                    raise subprocess.TimeoutExpired(args, 1, stderr=IAM.encode())
                if outcome == 'oserror':
                    raise OSError(IAM)
                return subprocess.CompletedProcess(args, int(outcome == 'failure'),
                    b'invalid' if outcome == 'invalid' else b'{}', IAM.encode())
            with self.subTest(outcome=outcome), patch.dict(os.environ, {
                    'HOME': '/synthetic/home', 'YC_TOKEN': 'synthetic-other',
                    'YC_IAM_TOKEN': 'synthetic-old-iam',
                    'RUNNER_REFERENCE_VALUE': 'inherited'}, clear=True), \
                    patch.object(deploy.subprocess, 'run', side_effect=execute), \
                    redirect_stdout(io.StringIO()) as output:
                yc = deploy.YandexCLI(self.settings, IAM, self.path.parent)
                if outcome == 'success':
                    self.assertEqual(yc.call(['test'], 'read test'), {})
                else:
                    with self.assertRaises(DeployError) as error:
                        yc.call(['test'], 'read test')
                    self.assertNotIn(IAM, str(error.exception))
                self.assertNotIn(IAM, output.getvalue())
                self.assertEqual(os.environ['YC_IAM_TOKEN'], 'synthetic-old-iam')
                self.assertEqual(os.environ['YC_TOKEN'], 'synthetic-other')
                self.assertTrue(paths)
                self.assertTrue(all(not p.exists() and not p.parent.exists() for p in paths))

    def test_cli_failure_reports_only_exit_and_fixed_reason(self):
        private = f'{IAM} private-contact@example.invalid'
        cases = [(b'code = PermissionDenied', 'permission_denied'),
                 (b'AccessDenied', 'permission_denied'),
                 (b'code = Unauthenticated', 'unauthenticated'),
                 (b'NoSuchBucket', 'not_found'),
                 (b'unknown flag:', 'unsupported_flag'),
                 (b'open /private: no such file or directory', 'local_file_unavailable'),
                 (b'unrecognized provider message', 'unknown')]
        for detail, reason in cases:
            with self.subTest(reason=reason), patch.object(deploy.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 7, private.encode(),
                        detail + b' ' + private.encode())):
                yc = deploy.YandexCLI(self.settings, IAM, self.path.parent)
                with self.assertRaises(DeployError) as caught:
                    yc.call(['test'], 'upload package')
                self.assertEqual(str(caught.exception),
                    f'cloud command failed: upload package; exit=7; reason={reason}')

    def test_upload_failure_stops_with_safe_diagnostics_without_extra_cloud_calls(self):
        private = f'{IAM} private-contact@example.invalid'.encode()
        for invalid_json in (False, True):
            with self.subTest(invalid_json=invalid_json):
                yc = FakeYC()
                fake_call = yc.call
                cli = deploy.YandexCLI(self.settings, IAM, self.path.parent)
                def call(args, label, **kwargs):
                    if 'put-object' in args:
                        yc.calls.append(args)
                        return cli.call(args, label, **kwargs)
                    return fake_call(args, label, **kwargs)
                yc.call = call
                upload = subprocess.CompletedProcess([], 0 if invalid_json else 1,
                    private if invalid_json else b'', b'AccessDenied ' + private)
                with patch.object(deploy.subprocess, 'run', return_value=upload) as command, \
                        self.assertRaises(DeployError) as caught:
                    self.publish(yc)
                message = str(caught.exception)
                self.assertIn('invalid cloud JSON: upload package' if invalid_json else
                              'exit=1; reason=permission_denied', message)
                self.assertNotIn(private.decode(), message)
                self.assertNotIn(IAM, message)
                self.assertNotIn('private-contact@example.invalid', message)
                self.assertEqual(sum('put-object' in a for a in yc.calls), 1)
                command.assert_called_once()
                self.assertFalse(any('head-object' in a for a in yc.calls))
                self.assertFalse(any('create' in a for a in yc.calls))

    def test_reference_upload_passes_existing_zip_path_to_cli_without_stdin(self):
        yc = FakeYC()
        cli = deploy.YandexCLI(self.settings, IAM, self.path.parent)
        fake_call = yc.call
        uploaded = []
        def execute(args, **kwargs):
            self.assertEqual(args[:3], ['yc', 'storage', 's3api'])
            body = args[args.index('--body') + 1]
            uploaded.append(Path(body).read_bytes())
            self.assertEqual(body, str(self.path))
            self.assertNotIn('input', kwargs)
            self.assertNotIn('stdin', kwargs)
            self.assertNotIn('cwd', kwargs)
            self.assertEqual(kwargs['env']['YC_IAM_TOKEN'], IAM)
            self.assertEqual(args[args.index('--bucket') + 1], self.settings.bucket)
            self.assertEqual(args[args.index('--key') + 1],
                             f'functions/zontcontrol/{SHA}/12-2.zip')
            return subprocess.CompletedProcess(args, 0, b'{}', b'')
        def call(args, label, **kwargs):
            if 'put-object' in args:
                return cli.call(args, label, **kwargs)
            return fake_call(args, label, **kwargs)
        yc.call = call
        with patch.object(deploy.subprocess, 'run', side_effect=execute) as command:
            self.assertEqual(self.publish(yc), 'd4' + 'v' * 18)
        command.assert_called_once()
        self.assertEqual(uploaded, [self.path.read_bytes()])


class DeployHTTPTests(unittest.TestCase):
    def test_http_boundary_names_do_not_expose_provider_errors(self):
        from urllib.error import HTTPError
        private = f'{IAM} private-contact@example.invalid'
        error = HTTPError('https://example.invalid/' + private, 401, private, {},
                          io.BytesIO(private.encode()))
        for stage in ('GitHub checks API', 'GitHub OIDC', 'Yandex IAM exchange'):
            with self.subTest(stage=stage), patch.object(support, 'build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(DeployError) as caught:
                    support.request_json(deploy.Request('https://example.invalid'), stage=stage)
                self.assertEqual(str(caught.exception), f'{stage} failed (status 401)')

    def test_oidc_names_the_failed_http_boundary(self):
        from urllib.error import HTTPError
        private = f'{IAM} private-contact@example.invalid'
        error = HTTPError('https://example.invalid', 401, private, {}, io.BytesIO(private.encode()))
        env = environment(Path('/unused.zip'))
        for fail_at, stage in ((0, 'GitHub OIDC'), (1, 'Yandex IAM exchange')):
            with self.subTest(stage=stage), patch.object(support, 'build_opener') as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.read.return_value = b'{"value":"synthetic-oidc"}'
                if fail_at:
                    opener.return_value.open.side_effect = [opener.return_value.open.return_value, error]
                else:
                    opener.return_value.open.side_effect = error
                with self.assertRaises(DeployError) as caught:
                    deploy.exchange_token(env, DEPLOY_SA)
                self.assertEqual(str(caught.exception), f'{stage} failed (status 401)')

    def test_http_errors_bodies_redirects_and_size_limits_are_private(self):
        from urllib.error import HTTPError, URLError
        from urllib.request import Request
        request = Request('https://example.invalid', headers={'Authorization': IAM})
        self.assertIsNone(support.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other'))
        for error in (HTTPError(request.full_url, 401, IAM, {}, io.BytesIO(IAM.encode())),
                      URLError(IAM), ValueError(IAM)):
            with self.subTest(error=type(error)), patch.object(support, 'build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(DeployError) as caught:
                    support.request_json(request)
                self.assertNotIn(IAM, str(caught.exception))
                self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 15)
        with patch.object(support, 'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = (
                b'x' * (support.MAX_RESPONSE_BYTES + 1))
            with self.assertRaises(DeployError):
                support.request_json(request)

    def test_partial_http_body_is_reported_without_private_bytes(self):
        from http.client import IncompleteRead
        from urllib.request import Request
        with patch.object(support, 'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.side_effect = (
                IncompleteRead(IAM.encode(), 1024))
            with self.assertRaises(DeployError) as error:
                support.request_json(Request('https://example.invalid'))
            self.assertNotIn(IAM, str(error.exception))

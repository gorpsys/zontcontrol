"""Verify the minimal cloud ZIP and run one synthetic once outside the checkout."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from scripts import build_yandex_archive as builder


SYNTHETIC_CONTACT = 'cloud-package@example.invalid'
SMOKE = r'''
import sys
from pathlib import Path
target = Path(sys.argv[1]).resolve()
assert Path.cwd().resolve() == target
sys.path.insert(0, str(target))
import os
assert 'ZONT_TOKEN' not in os.environ
def offline(event, args):
    if event.startswith('socket.'):
        raise AssertionError('network forbidden')
sys.addaudithook(offline)

from unittest.mock import Mock, patch
from contextlib import redirect_stdout
from io import StringIO
import importlib
import json
import logging
import pkgutil
with patch.object(Path, 'read_text', side_effect=AssertionError('import read config')), \
        patch.object(Path, 'read_bytes', side_effect=AssertionError('import read config')):
    import handler
    import zontcontrol
    from zontcontrol.cloud import handler as exported_handler
    assert handler.handler is exported_handler
    for module in pkgutil.walk_packages(zontcontrol.__path__, prefix='zontcontrol.'):
        importlib.import_module(module.name)
assert not logging.getLogger('zontcontrol').handlers
assert Path(zontcontrol.__file__).resolve().is_relative_to(target)
assert all('site-packages' not in entry for entry in sys.path)

from dataclasses import replace
from datetime import datetime, timezone
from zontcontrol.algorithms.boiler_reset import load_config
from zontcontrol.application import Application
from zontcontrol.autonomy.catalog import BOILER_RESET_SPEC
from zontcontrol.autonomy.contracts import Mode, ResultStatus
from zontcontrol.autonomy.registry import AlgorithmRegistry
from zontcontrol.config import load_controller_config, load_registry_config
from zontcontrol.controller.contracts import HistoryResult, SeriesKind, ValueDescriptor, ValueType
config = load_config()
assert load_controller_config().contact
calls = []
class Controller:
    def describe_values(self, device_id):
        assert device_id == config.device_id
        calls.append('describe')
        return tuple(ValueDescriptor(key, 'synthetic', None, 'synthetic', key.field,
            ValueType.BOOLEAN if key == config.reset_key else
            ValueType.INTEGER if key.field in ('worktime', 'target_sensor_id') else ValueType.NUMBER,
            True, True, SeriesKind.STEP) for key in config.history_keys)
    def read_history(self, device_id, keys, start, end):
        assert device_id == config.device_id and keys == config.history_keys
        calls.append('history')
        return HistoryResult(device_id, start, end, datetime.now(timezone.utc), ())
    def read_notes(self, device_id):
        assert device_id == config.device_id
        calls.append('notes_read')
        return json.dumps(['previous\n', 'older\n', 'expired\n'])
    def unexpected(self, *args, **kwargs):
        raise AssertionError('unexpected control operation')
    list_devices = read_current = prepare_command = execute_command = unexpected

controller = Controller()
registry = AlgorithmRegistry(load_registry_config(), controller,
    close_controller=lambda: calls.append('closed'))
app = Application(controller, registry)
try:
    registry.register_all((replace(BOILER_RESET_SPEC, modes=frozenset({Mode.ONCE})),))
    assert len(app.list_algorithms()) == 1
    assert app.list_continuous() == ()
    result = app.run_once('boiler_reset', timeout=300)
    assert result.status is ResultStatus.UNKNOWN
    assert result.value['assessment']['state_at_period_end'] == 'unknown'
    assert result.value['reset_performed'] is False
finally:
    assert app.close()
assert calls == ['describe', 'history', 'closed']

# Exercise the packaged handler too: publication failures preserve job success,
# so checking only the algorithm result would miss a broken notes composition.
calls.clear()
registry = AlgorithmRegistry(load_registry_config(), controller,
    close_controller=lambda: calls.append('closed'))
registry.register_all((replace(BOILER_RESET_SPEC, modes=frozenset({Mode.ONCE})),))
app = Application(controller, registry)
transport = Mock()
transport.request.return_value = {'ok': True}
output = StringIO()
with redirect_stdout(output), \
        patch('zontcontrol.cloud.create_application', return_value=app), \
        patch.dict(os.environ, {'ZONT_TOKEN': 'synthetic-offline'}), \
        patch('zontcontrol.application.HttpTransport', return_value=transport):
    report = handler.handler({}, None)
assert report['status'] == 'unknown'
assert report['notes_saved'] is True and report['notes_error'] is None
assert report['log'] == output.getvalue() and report['log']
assert calls == ['describe', 'history', 'notes_read', 'closed']
transport.request.assert_called_once()
request = transport.request.call_args.args[0]
assert request.full_url == 'https://my.zont.online/api/update_device'
payload = json.loads(request.data)
assert set(payload) == {'device_id', 'notes'} and payload['device_id'] == config.device_id
assert json.loads(payload['notes']) == [report['log'], 'previous\n', 'older\n']
transport.close.assert_called_once_with()
assert not any(name.startswith(('zontcontrol.algorithms.boiler_cli',
    'zontcontrol.algorithms.charts', 'zontcontrol.get_token', 'zontcontrol.__main__',
    'zontcontrol.cli')) for name in sys.modules)
print('cloud once smoke ok')
'''


def offline_environment():
    return {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT')
            if key in os.environ}


def smoke_package(entries):
    """Extract validated entries and invoke once/handler with synthetic metadata I/O."""
    with TemporaryDirectory(prefix='zontcontrol-cloud-smoke-') as directory:
        target = Path(directory)
        for name, data in entries.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        try:
            result = subprocess.run([sys.executable, '-I', '-S', '-c', SMOKE, str(target)],
                cwd=target, env=offline_environment(), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise builder.ArchiveError('Cloud package smoke unavailable') from None
        # Never echo child output: a corrupted artifact may print credentials or arbitrary errors.
        builder.require(result.returncode == 0 and result.stdout == 'cloud once smoke ok\n'
                        and not result.stderr, 'Cloud package offline smoke failed')


def check_package(root, path, *, contact=SYNTHETIC_CONTACT, expected_sha=None):
    sha, _dirty = builder.source_identity(root, expected_sha)
    entries = builder.read_archive(path)
    manifest = builder.verify_entries(entries, expected_sha=sha)
    expected = builder.prepare_entries(root, contact=contact)
    builder.require({name: data for name, data in entries.items() if name != builder.MANIFEST}
                    == expected, 'Cloud package checkout byte drift')
    smoke_package(entries)
    return {'status': 'ok', 'smoke': 'ok', 'source_sha': sha,
            'app_version': manifest['app_version'], 'dirty': manifest['dirty']}


def check_packages(root=builder.ROOT, expected_sha=None):
    with TemporaryDirectory(prefix='zontcontrol-cloud-package-') as directory:
        path = builder.build_archive(root, Path(directory) / 'function.zip',
                                     contact=SYNTHETIC_CONTACT, expected_sha=expected_sha)
        return check_package(root, path, expected_sha=expected_sha)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=builder.ROOT)
    parser.add_argument('--expected-sha')
    args = parser.parse_args(arguments)
    try:
        print(json.dumps(check_packages(args.project_root, args.expected_sha), sort_keys=True))
        return 0
    except (builder.ArchiveError, OSError) as error:
        reason = 'Local files unavailable' if isinstance(error, OSError) else str(error)
        print(f'Cloud package check failed: {reason}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

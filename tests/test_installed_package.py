"""Source contracts plus a wheel-only suite run by ``make package-check``.

The driver installs an already built wheel with no index/dependencies in a new
venv, then runs this file with -I from a temporary application root. The installed
process has neither the checkout on sys.path nor credentials in its environment.
"""

import ast
from importlib import import_module, metadata, util
import inspect
import json
import os
from pathlib import Path
import pkgutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import venv
import zipfile

import zontcontrol


INSTALLED = '--installed' in sys.argv
if INSTALLED:
    sys.argv.remove('--installed')


def offline_environment():
    # Build a fresh allowlist: never pass a caller's token or config overrides.
    return {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT')
            if key in os.environ}


class PackageContentsTests(unittest.TestCase):
    def test_nested_modules_import_without_network_or_prototype(self):
        required = {'zontcontrol.application', 'zontcontrol.controller.auth',
            'zontcontrol.controller.contracts', 'zontcontrol.controller.transport',
            'zontcontrol.controller.zont', 'zontcontrol.controller.service',
            'zontcontrol.autonomy.registry', 'zontcontrol.autonomy.runtime',
            'zontcontrol.algorithms.boiler_reset', 'zontcontrol.algorithms.boiler_cli',
            'zontcontrol.algorithms.charts'}
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            modules = {item.name for item in pkgutil.walk_packages(
                zontcontrol.__path__, prefix='zontcontrol.')}
            self.assertLessEqual(required, modules)
            for name in modules:
                if name != 'zontcontrol.__main__':  # executed separately as an entrypoint
                    import_module(name)
            for name in ('client', 'cli', 'control'):
                self.assertIsNone(util.find_spec(f'zontcontrol.{name}'))
        for path in Path(zontcontrol.__file__).parent.rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            forbidden = {node.name for node in ast.walk(tree)
                         if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
            self.assertFalse({'ZontClient', 'apply_command'} & forbidden, path.name)

    def test_source_and_wheel_expose_the_same_direct_command_contract(self):
        from zontcontrol.controller.contracts import API_VERSION, Controller
        self.assertEqual(API_VERSION, 2)
        self.assertEqual(tuple(inspect.signature(Controller.execute_command).parameters),
                         ('self', 'command'))
        for module in ('coordination', 'controller.ownership', 'controller.requests'):
            self.assertIsNone(util.find_spec('zontcontrol.'+module))


@unittest.skipUnless(INSTALLED, 'installed wheel suite: make package-check')
class InstalledPackageTests(unittest.TestCase):
    def test_distribution_is_isolated_stdlib_runtime_and_has_only_current_entrypoints(self):
        distribution = metadata.distribution('zontcontrol')
        self.assertEqual(distribution.version, zontcontrol.__version__)
        self.assertEqual(distribution.requires or [], [])
        self.assertNotIn(Path(__file__).resolve().parents[1],
                         [Path(path).resolve() for path in sys.path])
        self.assertIn('site-packages', str(Path(zontcontrol.__file__).resolve()))
        entrypoints = {item.name: item.value for item in distribution.entry_points
                       if item.group == 'console_scripts'}
        self.assertEqual(entrypoints, {'zontcontrol': 'zontcontrol.application:main',
            'zontcontrol-get-token': 'zontcontrol.get_token:main'})

    def test_no_working_configs_credentials_reports_or_development_files_in_wheel(self):
        files = metadata.distribution('zontcontrol').files
        self.assertIsNotNone(files)
        for item in files:
            parts = Path(item).parts
            self.assertFalse({'config', 'reports', 'tests', 'scripts', '.git'} & set(parts), item)
            self.assertFalse(any(part == '.env' or part.startswith('.env.') for part in parts))

    def test_all_installed_entrypoints_and_python_modules_help_and_version_without_credentials(self):
        binary = Path(sys.executable).parent
        commands = [[str(binary / name)] for name in ('zontcontrol', 'zontcontrol-get-token')]
        commands += [[sys.executable, '-I', '-m', name]
                     for name in ('zontcontrol', 'zontcontrol.get_token')]
        with TemporaryDirectory() as directory:
            for command in commands:
                for flag in ('--help', '--version'):
                    with self.subTest(command=command, flag=flag):
                        result = subprocess.run([*command, flag], cwd=directory,
                            env=offline_environment(), capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertTrue(result.stdout)
                        if flag == '--version':
                            self.assertIn(zontcontrol.__version__, result.stdout)
            result = subprocess.run([str(binary / 'zontcontrol'), 'list'], cwd=directory,
                env=offline_environment(), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Set ZONT_TOKEN', result.stderr)

    def test_components_read_fixed_files_from_application_root_and_fail_when_missing(self):
        from zontcontrol.algorithms.boiler_cli import load_config as cli_config
        from zontcontrol.algorithms.boiler_reset import load_config as reset_config
        from zontcontrol.application import create_application
        from zontcontrol.autonomy.contracts import AlgorithmState
        from zontcontrol.config import ConfigurationError, load_controller_config, load_registry_config

        controller = {'contact': 'installed@example.invalid', 'http_timeout_seconds': 7}
        registry = {'disabled_modules': [], 'once_timeouts': {
            'boiler_reset': {'minimum_seconds': 170, 'recommended_seconds': 180}}}
        reset = {'device_id': 2042, 'circuit_id': 10, 'air_sensor_id': 7,
            'coolant_sensor_id': 8, 'reset_control_id': 31, 'check_interval_seconds': 61}
        cli = {'devices': [2042], 'controls': [], 'graphs': []}
        original = Path.cwd()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir()
            for name, data in (('controller', controller), ('registry', registry),
                               ('boiler_reset', reset), ('boiler_cli', cli)):
                (root / f'config/{name}.json').write_text(json.dumps(data), encoding='utf-8')
            os.chdir(root)
            try:
                self.assertEqual(load_controller_config().contact, 'installed@example.invalid')
                self.assertEqual(load_registry_config().once_timeouts['boiler_reset'].recommended_seconds, 180)
                self.assertEqual(reset_config().device_id, 2042)
                self.assertEqual(cli_config().devices, (2042,))
                with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
                        patch('zontcontrol.application.HttpTransport') as transport, \
                        patch('zontcontrol.algorithms.boiler_cli.Terminal.available', return_value=False):
                    with create_application(environ={'ZONT_TOKEN': 'synthetic-offline'}) as app:
                        rows = {row.spec.algorithm_id: row for row in app.list_algorithms()}
                        self.assertIs(rows['boiler_reset'].state, AlgorithmState.REGISTERED)
                        self.assertIs(rows['boiler_cli'].state, AlgorithmState.FAILED)
                        self.assertEqual(app._controller._adapter._transport, transport.return_value)
                    transport.return_value.request.assert_not_called()
                for name, load in (('boiler_reset', reset_config), ('boiler_cli', cli_config),
                                   ('registry', load_registry_config),
                                   ('controller', load_controller_config)):
                    (root / f'config/{name}.json').unlink()
                    with self.subTest(name=name), self.assertRaises(ConfigurationError):
                        load()
            finally:
                os.chdir(original)


def check_wheel(wheel):
    """Build tooling is external; install and application checks use no network."""
    wheel = Path(wheel).resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if any(name.startswith(('config/', 'reports/', 'tests/', 'scripts/')) for name in names):
            raise AssertionError('wheel includes development data or working configs')
    with TemporaryDirectory(prefix='zontcontrol-installed-') as directory:
        root = Path(directory)
        venv.EnvBuilder(with_pip=True).create(root / 'venv')
        python = root / 'venv/bin/python'
        subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--no-index',
            '--no-deps', '--no-cache-dir', str(wheel)], env=offline_environment(), check=True,
            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        app_root = root / 'application'
        app_root.mkdir()
        subprocess.run([str(python), '-I', str(Path(__file__).resolve()), '--installed', '-v'],
                       cwd=app_root, env=offline_environment(), check=True)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--wheel':
        check_wheel(sys.argv[2])
    else:
        unittest.main()

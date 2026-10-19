"""Offline configuration boundaries; factories stand in for future modules."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from zontcontrol.autonomy.contracts import (
    AlgorithmResult, AlgorithmSpec, AlgorithmState, Mode, ResultStatus,
    validate_catalog,
)
from zontcontrol.config import (
    ConfigurationError, load_controller_config, load_registry_config, read_json,
)
from zontcontrol.algorithms.boiler_cli import load_config as load_cli_config


def spec(algorithm_id='reset', modes=frozenset({Mode.ONCE, Mode.CONTINUOUS}),
         minimum=40, requirements=frozenset({'read_current'}), factory=None):
    return AlgorithmSpec(algorithm_id, 'Test module', modes, factory or Mock(),
                         requirements, minimum)


class ConfigurationTests(unittest.TestCase):
    def test_example_pacing_is_a_feasible_calibration_grid_member(self):
        from scripts.calibrate_coordination import Safety, Pacing, candidates
        project = Path(__file__).resolve().parents[1]
        config = load_controller_config(reader=lambda path: read_json(
            project/'config/controller.example.json'))
        settings = config.coordination
        pacing = Pacing(settings.poll_seconds, settings.retry_seconds,
                        settings.jitter_fraction, settings.renewal_fraction)
        safety = Safety(delta=settings.delta_seconds, delta_cmd=settings.delta_cmd_seconds,
                        period=settings.leader_period_seconds)
        self.assertIn(pacing, list(candidates(safety)))
        self.assertIn('unmeasured', config.statistics.profile)

    def test_cli_existing_json_uses_default_and_new_wait_is_module_owned(self):
        self.write('boiler_cli.json', {'devices': [42], 'controls': [], 'graphs': [],
                                     'lease_seconds': 20})
        self.assertEqual(load_cli_config(root=self.root).grant_wait_seconds, 10)
        for value in (0, True, 21, '5'):
            self.write('boiler_cli.json', {'devices': [42], 'controls': [], 'graphs': [],
                'lease_seconds': 20, 'grant_wait_seconds': value})
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                load_cli_config(root=self.root)
        self.write('boiler_cli.json', {'devices': [42], 'controls': [], 'graphs': [],
                                     'lease_seconds': 20, 'grant_wait_seconds': 5})
        self.assertEqual(load_cli_config(root=self.root).grant_wait_seconds, 5)

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'config').mkdir()

    def write(self, name, value):
        path = self.root / 'config' / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def registry(self, **overrides):
        value = {'once_timeouts': {'reset': {
            'minimum_seconds': 45, 'recommended_seconds': 90}}}
        value.update(overrides)
        self.write('registry.json', value)
        return load_registry_config(root=self.root)

    def controller(self, **overrides):
        value = {'contact': 'owner@example.invalid', 'http_timeout_seconds': 15,
                 'coordination': {'delta_seconds': 1, 'leader_period_seconds': 300, 'delta_cmd_seconds': 1,
                                  'profile_id': 'ea3221c0-2a98-49f0-8705-e81917b9b5a9',
                                  'poll_seconds': .2, 'retry_seconds': .5,
                                  'jitter_fraction': 0, 'renewal_fraction': .33},
                 'statistics': {'path': 'reports/orchestration.jsonl', 'profile': 'unmeasured'}}
        value.update(overrides)
        self.write('controller.json', value)
        return load_controller_config(root=self.root)

    def test_fixed_separate_files_and_injected_reader(self):
        calls = []

        def reader(path):
            calls.append(path)
            return {'once_timeouts': {}}

        config = load_registry_config(root=self.root, reader=reader)
        self.assertEqual(calls, [self.root / 'config/registry.json'])
        self.assertFalse(config.autostart_dual_mode)
        self.assertEqual(config.disabled_modules, frozenset())
        controller = self.controller()
        self.assertEqual(controller.http_timeout_seconds, 15)
        self.assertEqual(controller.coordination.timing.clock_rho, .05)
        self.assertEqual(controller.statistics.profile, 'unmeasured')
        with self.assertRaisesRegex(ConfigurationError, 'controller.json'):
            load_controller_config(root=self.root, reader=reader)

    def test_missing_and_malformed_json_show_path_and_instructions(self):
        missing = self.root / 'config/boiler_reset.json'
        with self.assertRaisesRegex(ConfigurationError, 'config/boiler_reset.json.*Create'):
            read_json(missing)
        for raw in ('{', '[]', '{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}',
                    '{"x":1e999}', '\udcff'):
            with self.subTest(raw=repr(raw)):
                path = self.root / 'config/module.json'
                path.write_bytes(raw.encode('utf-8', errors='surrogateescape'))
                with self.assertRaisesRegex(ConfigurationError, 'module.json'):
                    read_json(path)

    def test_wire_v2_requires_explicit_shared_profile_and_pacing(self):
        with self.assertRaisesRegex(ConfigurationError, 'wire v2.*controller.example.json'):
            self.controller(coordination={'delta_seconds': 1, 'leader_period_seconds': 60})
        config = self.controller()
        good = dict(delta_seconds=1, delta_cmd_seconds=1, leader_period_seconds=300,
                    profile_id=config.coordination.profile_id, poll_seconds=.2, retry_seconds=.5,
                    jitter_fraction=.1, renewal_fraction=.33)
        for change in ({'profile_id': 'bad'}, {'profile_id': '0'*32}, {'delta_cmd_seconds': True},
                       {'poll_seconds': 0}, {'retry_seconds': False}, {'jitter_fraction': 1.01},
                       {'jitter_fraction': float('nan')}, {'renewal_fraction': 0},
                       {'renewal_fraction': .999}, {'poll_seconds': 300}):
            with self.subTest(change=change), self.assertRaises(ConfigurationError):
                self.controller(coordination={**good, **change})

    def test_previous_profile_is_explicit_exact_and_has_distinct_uuid(self):
        from dataclasses import asdict
        current = self.controller().coordination
        previous = dict(profile_id='c935f016-9ea1-4909-8f10-96fd98c792cb',
                        delta_seconds=5, delta_cmd_seconds=10, rho_version=1)
        config = self.controller(coordination={**{key: value for key, value in asdict(current).items()
                                                  if key != 'previous_profile'},
                                                'previous_profile': previous})
        self.assertEqual(asdict(config.coordination.previous_profile), previous)
        for bad in ({**previous, 'profile_id': current.profile_id},
                    {**previous, 'rho_version': 2}, {**previous, 'extra': 1},
                    {k: v for k, v in previous.items() if k != 'delta_cmd_seconds'}):
            with self.assertRaises(ConfigurationError):
                self.controller(coordination={**asdict(current), 'previous_profile': bad})

    def test_controller_validation_precedes_any_effect_and_hides_values(self):
        invalid = [
            {'token': 'private-value'}, {'password': 'private-value'},
            {'contact': ''}, {'contact': 'bad\nheader'}, {'contact': '\u2603'},
            {'http_timeout_seconds': True}, {'http_timeout_seconds': 0},
            {'http_timeout_seconds': 121}, {'http_timeout_seconds': float('inf')},
            {'coordination': {'delta_seconds': 10, 'leader_period_seconds': 5}},
            {'coordination': {'delta_seconds': -1, 'leader_period_seconds': 60}},
            {'coordination': {'delta_seconds': 1, 'leader_period_seconds': 60, 'clock_rho': 0}},
            {'statistics': {'path': '', 'profile': 'unmeasured'}},
            {'statistics': {'path': 'reports/x.jsonl', 'profile': ''}},
        ]
        with patch('urllib.request.OpenerDirector.open') as network:
            for values in invalid:
                with self.subTest(values=values):
                    with self.assertRaises(ConfigurationError) as caught:
                        self.controller(**values)
                    self.assertIn('controller.json', str(caught.exception))
                    self.assertNotIn('private-value', str(caught.exception))
            network.assert_not_called()
        self.assertFalse((self.root / 'reports').exists())

    def test_registry_does_not_accept_module_schema_or_credentials(self):
        for extra in ({'module_configs': {}}, {'token': 'private-value'},
                      {'autostart_dual_mode': 1}, {'disabled_modules': ['reset', 'reset']},
                      {'disabled_modules': 'reset'}, {'disabled_modules': ['../reset']}):
            with self.subTest(extra=extra), self.assertRaises(ConfigurationError):
                self.registry(**extra)

    def test_once_effective_minimum_and_recommended(self):
        config = self.registry()
        pair = config.timeout_for(spec(minimum=50))
        self.assertEqual((pair.minimum_seconds, pair.recommended_seconds), (50, 90))
        self.assertEqual(pair.resolve(None), 90)
        self.assertEqual(pair.resolve(50), 50)
        for value in (0, -1, True, float('inf'), float('nan'), 49, '90'):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                pair.resolve(value)
        with self.assertRaises(ConfigurationError):
            config.timeout_for(spec(minimum=100))

    def test_missing_pair_fails_only_that_once_validation(self):
        config = self.registry(once_timeouts={})
        with self.assertRaisesRegex(ConfigurationError, 'reset'):
            config.timeout_for(spec())
        self.assertIsNone(config.timeout_for(spec(modes=frozenset({Mode.CONTINUOUS}), minimum=0)))

    def test_incomplete_or_invalid_timeout_pairs(self):
        for pair in ({}, {'minimum_seconds': 20}, {'recommended_seconds': 50},
                     {'minimum_seconds': 50, 'recommended_seconds': 20},
                     {'minimum_seconds': False, 'recommended_seconds': 50},
                     {'minimum_seconds': 1, 'recommended_seconds': float('nan')},
                     {'minimum_seconds': 1, 'recommended_seconds': 3, 'extra': 4}):
            with self.subTest(pair=pair), self.assertRaises(ConfigurationError):
                self.registry(once_timeouts={'reset': pair})

    def test_disabled_factory_and_its_own_file_are_never_used(self):
        factory = Mock(side_effect=lambda controller: read_json(
            self.root / 'config/forbidden.json'))
        denied = spec('forbidden', factory=factory)
        config = self.registry(disabled_modules=['forbidden'])
        allowed = validate_catalog((spec(), denied), config)
        self.assertEqual([s.algorithm_id for s in allowed], ['reset'])
        for selected in allowed:
            selected.factory(Mock())
        factory.assert_not_called()

    def test_unknown_ids_and_duplicates_are_not_silently_ignored(self):
        for config in (self.registry(disabled_modules=['typo']),
                       self.registry(once_timeouts={'typo': {
                           'minimum_seconds': 1, 'recommended_seconds': 2}})):
            with self.assertRaisesRegex(ConfigurationError, 'Unknown'):
                validate_catalog((spec(),), config)
        with self.assertRaisesRegex(ConfigurationError, 'Duplicate'):
            validate_catalog((spec(), spec()), self.registry())

    def test_capabilities_checked_per_enabled_spec_without_factory(self):
        module = spec()
        with self.assertRaisesRegex(ValueError, 'read_current'):
            module.validate_requirements(frozenset({'list_devices'}))
        module.validate_requirements(frozenset({'read_current'}))
        module.factory.assert_not_called()

    def test_modules_own_configuration_and_fail_before_their_network(self):
        network = Mock()

        def factory(controller):
            path = self.root / 'config/reset.json'
            values = read_json(path)
            if type(values.get('interval')) is not int or values['interval'] <= 0:
                raise ConfigurationError(path, 'Invalid interval')
            network()
            return Mock()

        module = spec(factory=factory)
        self.write('reset.json', {'interval': 0})
        allowed = validate_catalog((module,), self.registry())
        with self.assertRaisesRegex(ConfigurationError, 'reset.json'):
            allowed[0].factory(Mock())
        network.assert_not_called()

    def test_checked_in_examples_are_valid_and_headless_disables_cli(self):
        project = Path(__file__).resolve().parents[1]
        for name in ('registry.example.json', 'registry.headless.example.json'):
            config = load_registry_config(reader=lambda _path: read_json(project / 'config' / name))
            if 'headless' in name:
                self.assertIn('boiler_cli', config.disabled_modules)
            self.assertFalse(config.autostart_dual_mode)
        config = load_controller_config(reader=lambda _path: read_json(
            project / 'config/controller.example.json'))
        self.assertEqual(config.statistics.profile, 'unmeasured-example')

    def test_modes_spec_and_autostart_contract(self):
        config = self.registry()
        dual = spec()
        once = spec(modes=frozenset({Mode.ONCE}))
        continuous = spec(modes=frozenset({Mode.CONTINUOUS}), minimum=0)
        self.assertFalse(config.should_autostart(dual))
        self.assertFalse(config.should_autostart(once))
        self.assertTrue(config.should_autostart(continuous))
        enabled = self.registry(autostart_dual_mode=True)
        self.assertTrue(enabled.should_autostart(dual))
        self.assertFalse(enabled.should_autostart(once))
        self.assertTrue(enabled.should_autostart(continuous))
        for values in ({'algorithm_id': '../bad'}, {'modes': frozenset()},
                       {'modes': frozenset({'once'})}, {'minimum': -1},
                       {'minimum': True}, {'factory': 3},
                       {'requirements': frozenset({'invented'})}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                spec(**values)

    def test_factory_result_and_state_contracts_are_transport_free(self):
        result = AlgorithmResult(ResultStatus.UNKNOWN, {'reason': 'insufficient history'})
        algorithm = Mock()
        algorithm.run_once.return_value = result
        factory = Mock(return_value=algorithm)
        controller = Mock()
        instance = spec(factory=factory).factory(controller)
        factory.assert_called_once_with(controller)
        self.assertIs(instance.run_once(Mock()), result)
        self.assertEqual(AlgorithmState.STOPPING.value, 'stopping')
        self.assertEqual(AlgorithmState.FAILED.value, 'failed')
        with self.assertRaises(ValueError):
            AlgorithmResult('success')


if __name__ == '__main__':
    unittest.main()

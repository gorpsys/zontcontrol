"""Allowlisted cloud builds from synthetic checkouts, never working configs."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from scripts import build_yandex_archive as builder
from zontcontrol import __version__


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SOURCES = {'handler.py'} | {
    f'zontcontrol/{name}.py' for name in (
        '__init__', 'application', 'cloud', 'config', 'logging_config', 'anomaly',
        'detection_profile', 'heating', 'heating_adapter', 'monitor', 'commands',
        'history', 'inventory')} | {
    f'zontcontrol/{package}/{name}.py' for package, names in (
        ('algorithms', ('__init__', 'boiler_reset')),
        ('autonomy', ('__init__', 'catalog', 'contracts', 'registry', 'runtime')),
        ('controller', ('__init__', 'auth', 'contracts', 'service', 'transport', 'zont')))
    for name in names}
CONFIG_NAMES = ('controller', 'registry', 'boiler_reset')
EXPECTED_MEMBERS = EXPECTED_SOURCES | {f'config/{name}.json' for name in CONFIG_NAMES} | {
    'build-manifest.json'}
CONTACT = 'cloud-package@example.invalid'


class ArchiveFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        for name in EXPECTED_SOURCES | {f'deploy/cloud-config/{n}.json' for n in CONFIG_NAMES}:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.git('init', '-q')
        self.git('add', '.')
        self.git('-c', 'user.name=Offline test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'fixture')
        self.sha = self.git('rev-parse', 'HEAD').strip()
        self.output = self.root / 'build/function.zip'
        self.stack.enter_context(patch.dict(os.environ, {'ZONT_CONTACT': CONTACT}, clear=True))

    def git(self, *arguments):
        environment = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ}
        environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
        return subprocess.check_output(['git', *arguments], cwd=self.root, text=True,
                                       env=environment, stderr=subprocess.PIPE)

    def build(self, **kwargs):
        return builder.build_archive(self.root, self.output, **kwargs)

    def entries(self):
        with zipfile.ZipFile(self.output) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def change_config(self, name, update):
        path = self.root / f'deploy/cloud-config/{name}.json'
        data = json.loads(path.read_text())
        update(data)
        path.write_text(json.dumps(data), encoding='utf-8')


class YandexArchiveTests(ArchiveFixture):
    def test_exact_allowlist_bytes_and_manifest_from_clean_head(self):
        self.build(expected_sha=self.sha)
        entries = self.entries()
        self.assertEqual(set(entries), EXPECTED_MEMBERS)
        for name in EXPECTED_SOURCES:
            self.assertEqual(entries[name], (self.root / name).read_bytes())
        manifest = json.loads(entries.pop('build-manifest.json'))
        self.assertEqual(manifest, {'schema_version': 1, 'app_version': __version__,
            'source_sha': self.sha, 'dirty': False,
            'files': {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}})
        self.assertEqual(json.loads(entries['config/controller.json']), {
            'contact': CONTACT, 'http_timeout_seconds': 15})
        self.assertEqual(json.loads((self.root / 'deploy/cloud-config/controller.json').read_text())[
            'contact'], '')

    def test_missing_or_invalid_contact_fails_without_creating_output(self):
        for contact in (None, '', '  ', 'line\nbreak', 'не ascii'):
            with self.subTest(contact=contact), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(builder.ArchiveError):
                    self.build(contact=contact)
                self.assertFalse(self.output.exists())

    def test_all_cloud_configs_use_existing_validators_before_output(self):
        cases = [('controller', lambda d: d.update(extra=True)),
                 ('controller', lambda d: d.pop('contact')),
                 ('controller', lambda d: d.update(http_timeout_seconds=121)),
                 ('registry', lambda d: d.update(once_timeouts={})),
                 ('registry', lambda d: d.update(disabled_modules=['boiler_reset'])),
                 ('registry', lambda d: d.update(once_timeouts={
                     'boiler_reset': {'minimum_seconds': 400, 'recommended_seconds': 400}})),
                 ('registry', lambda d: d.update(unknown=True)),
                 ('boiler_reset', lambda d: d.update(device_id=0)),
                 ('boiler_reset', lambda d: d.update(unknown=True))]
        for name, update in cases:
            path = self.root / f'deploy/cloud-config/{name}.json'
            original = path.read_bytes()
            self.change_config(name, update)
            with self.subTest(name=name), self.assertRaises(builder.ArchiveError):
                self.build()
            self.assertFalse(self.output.exists())
            path.write_bytes(original)

    def test_duplicate_keys_nonfinite_and_invalid_json_are_rejected(self):
        path = self.root / 'deploy/cloud-config/controller.json'
        for data in ('{} trailing', '{"contact":"","contact":""}',
                     '{"contact":"","http_timeout_seconds":NaN}'):
            path.write_text(data, encoding='utf-8')
            with self.subTest(data=data), self.assertRaises(builder.ArchiveError):
                self.build()

    def test_payload_preparation_reads_neither_token_nor_working_configs(self):
        original_read = Path.read_bytes
        read_paths = []
        def read_input(path):
            read_paths.append(path.relative_to(self.root).as_posix())
            return original_read(path)
        class Environment(dict):
            def get(self, key, *default):
                if key != 'ZONT_CONTACT':
                    raise AssertionError('unexpected environment access')
                return super().get(key, *default)
        with patch.object(Path, 'read_bytes', autospec=True, side_effect=read_input), \
                patch.object(builder.os, 'environ', Environment(ZONT_CONTACT=CONTACT)), \
                patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
            builder.prepare_entries(self.root)
        self.assertEqual(set(read_paths), EXPECTED_SOURCES | {
            f'deploy/cloud-config/{name}.json' for name in CONFIG_NAMES})

    def test_output_symlinks_and_head_change_preserve_previous_archive(self):
        self.build()
        previous = self.output.read_bytes()
        linked = self.output.with_name('linked.zip')
        linked.symlink_to(self.output)
        with self.assertRaises(builder.ArchiveError):
            builder.build_archive(self.root, linked)
        identities = [[(self.sha, False), (self.sha, True)],
                      [(self.sha, True), (self.sha, True), ('a' * 40, True)]]
        for sequence in identities:
            with self.subTest(sequence=sequence), patch.object(
                    builder, 'source_identity', side_effect=sequence):
                with self.assertRaises(builder.ArchiveError):
                    self.build()
        self.assertEqual(self.output.read_bytes(), previous)

    def test_environment_sha_must_be_full_current_head(self):
        for sha in ('f' * 40, self.sha[:8], 'bad'):
            with self.subTest(sha=sha), patch.dict(os.environ, {'CLOUD_PACKAGE_SHA': sha}):
                with self.assertRaises(builder.ArchiveError):
                    self.build()
        with patch.dict(os.environ, {'CLOUD_PACKAGE_SHA': self.sha}):
            self.build()

    def test_dirty_source_is_explicit_and_untracked_files_are_excluded(self):
        (self.root / 'handler.py').write_text((self.root / 'handler.py').read_text() + '\n')
        for name in ('private.json', 'config/controller.json', 'config/boiler_cli.json',
                     'zontcontrol/unrelated.py', 'tests/extra.py', 'docs/extra.md'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('excluded local data', encoding='utf-8')
        self.build()
        entries = self.entries()
        self.assertEqual(set(entries), EXPECTED_MEMBERS)
        self.assertTrue(json.loads(entries['build-manifest.json'])['dirty'])
        self.assertEqual(json.loads(entries['config/controller.json'])['contact'], CONTACT)

    def test_symlink_sources_profile_and_parent_directories_are_rejected(self):
        for name in ('handler.py', 'deploy/cloud-config/controller.json', 'zontcontrol/autonomy'):
            path = self.root / name
            moved = path.with_name(path.name + '-real')
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=moved.is_dir())
            try:
                with self.subTest(name=name), self.assertRaises(builder.ArchiveError):
                    self.build()
                self.assertFalse(self.output.exists())
            finally:
                path.unlink()
                moved.rename(path)

    def test_missing_source_and_bad_contact_preserve_previous_zip(self):
        self.build()
        previous = self.output.read_bytes()
        (self.root / 'handler.py').unlink()
        with self.assertRaises(builder.ArchiveError):
            self.build()
        self.assertEqual(self.output.read_bytes(), previous)
        with self.assertRaises(builder.ArchiveError):
            self.build(contact='')
        self.assertEqual(self.output.read_bytes(), previous)

    def test_corrupt_staged_zip_does_not_replace_previous_zip_or_leave_temporary_files(self):
        self.build()
        previous = self.output.read_bytes()
        write_archive = builder._write_archive
        def corrupt(path, entries):
            write_archive(path, entries)
            path.write_bytes(b'broken zip')
        with patch.object(builder, '_write_archive', side_effect=corrupt):
            with self.assertRaises(builder.ArchiveError):
                self.build()
        self.assertEqual(self.output.read_bytes(), previous)
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_staged_content_drift_is_rejected_even_with_updated_manifest(self):
        self.build()
        previous = self.output.read_bytes()
        write_archive = builder._write_archive
        def drift(path, entries):
            changed = dict(entries, **{'handler.py': b'# different bytes\n'})
            manifest = json.loads(changed['build-manifest.json'])
            manifest['files']['handler.py'] = hashlib.sha256(changed['handler.py']).hexdigest()
            changed['build-manifest.json'] = json.dumps(manifest).encode()
            write_archive(path, changed)
        with patch.object(builder, '_write_archive', side_effect=drift):
            with self.assertRaises(builder.ArchiveError):
                self.build()
        self.assertEqual(self.output.read_bytes(), previous)


if __name__ == '__main__':
    unittest.main()

"""ZIP integrity, provenance and isolated stdlib-only once smoke."""

import hashlib
import json
import stat
import unittest
import warnings
import zipfile

from scripts import build_yandex_archive as builder
from scripts import check_cloud_package as checker
from tests.test_yandex_archive import ArchiveFixture, CONTACT, ROOT


class CloudPackageTests(ArchiveFixture):
    def rewrite(self, entries, *, duplicate=None, symlink=None):
        with zipfile.ZipFile(self.output, 'w') as archive:
            for name, data in entries.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = ((stat.S_IFLNK if name == symlink else stat.S_IFREG) | 0o644) << 16
                archive.writestr(info, data)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(duplicate, entries[duplicate])

    def test_clean_package_accepts_expected_sha_and_clean_requirement(self):
        self.build()
        manifest = builder.verify_archive(self.output, expected_sha=self.sha, require_clean=True)
        self.assertEqual(manifest['source_sha'], self.sha)
        checker.check_package(self.root, self.output, contact=CONTACT, expected_sha=self.sha)

    def test_dirty_package_rejected_for_deploy_but_permitted_locally(self):
        (self.root / 'handler.py').write_text('# local edit\n')
        self.build()
        self.assertTrue(builder.verify_archive(self.output)['dirty'])
        with self.assertRaises(builder.ArchiveError):
            builder.verify_archive(self.output, require_clean=True)

    def test_manifest_schema_version_sha_version_and_file_hash_drift_are_rejected(self):
        self.build()
        original = self.entries()
        updates = [lambda d: d.update(schema_version=True),
                   lambda d: d.update(app_version='99.0.0'),
                   lambda d: d.update(source_sha='a' * 40),
                   lambda d: d.update(dirty=0),
                   lambda d: d.update(files={}),
                   lambda d: d['files'].update({'handler.py': '0' * 64}),
                   lambda d: d.update(unexpected=True)]
        for update in updates:
            manifest = json.loads(original['build-manifest.json'])
            update(manifest)
            entries = dict(original, **{'build-manifest.json': json.dumps(manifest).encode()})
            self.rewrite(entries)
            with self.subTest(update=update), self.assertRaises(builder.ArchiveError):
                builder.verify_archive(self.output, expected_sha=self.sha)

    def test_exact_composition_rejects_missing_extra_traversal_duplicate_and_symlink_members(self):
        self.build()
        original = self.entries()
        missing = dict(original)
        missing.pop('handler.py')
        for entries, kwargs in ((missing, {}),
                (dict(original, **{'private.json': b'extra'}), {}),
                (dict(original, **{'../escape.py': b'escape'}), {}),
                (original, {'duplicate': 'handler.py'}), (original, {'symlink': 'handler.py'})):
            with self.subTest(kwargs=kwargs):
                self.rewrite(entries, **kwargs)
                with self.assertRaises(builder.ArchiveError):
                    builder.verify_archive(self.output)

    def test_crc_is_checked_and_symlink_archives_are_rejected(self):
        self.build()
        self.rewrite(self.entries())
        with zipfile.ZipFile(self.output) as archive:
            entry = archive.getinfo('handler.py')
            offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
        data = bytearray(self.output.read_bytes())
        data[offset] ^= 1
        self.output.write_bytes(data)
        with self.assertRaises(builder.ArchiveError):
            builder.verify_archive(self.output)
        linked = self.output.with_name('linked.zip')
        linked.symlink_to(self.output)
        with self.assertRaises(builder.ArchiveError):
            builder.verify_archive(linked)

    def test_checker_compares_checkout_bytes_even_after_hashes_are_updated(self):
        self.build()
        entries = self.entries()
        entries['handler.py'] += b'\n# drift\n'
        manifest = json.loads(entries['build-manifest.json'])
        manifest['files']['handler.py'] = hashlib.sha256(entries['handler.py']).hexdigest()
        entries['build-manifest.json'] = json.dumps(manifest).encode()
        self.rewrite(entries)
        with self.assertRaises(builder.ArchiveError):
            checker.check_package(self.root, self.output, contact=CONTACT)

    def test_offline_import_and_real_once_with_fake_controller_outside_checkout(self):
        self.build()
        facts = checker.check_package(self.root, self.output, contact=CONTACT)
        self.assertEqual(facts['smoke'], 'ok')
        self.assertEqual(facts['source_sha'], self.sha)

    def test_smoke_rejects_import_network_attempt_without_calling_production_handler(self):
        path = self.root / 'handler.py'
        path.write_text("import socket\nsocket.create_connection(('example.invalid', 443))\n")
        self.build()
        with self.assertRaises(builder.ArchiveError):
            checker.check_package(self.root, self.output, contact=CONTACT)


class CloudCheckWiringTests(unittest.TestCase):
    def test_make_and_existing_python_matrix_include_cloud_smoke(self):
        workflow = (ROOT / '.github/workflows/checks.yml').read_text()
        self.assertIn('make cloud-package-check PYTHON=python', workflow)
        self.assertIn("python: ['3.11', '3.12']", workflow)
        makefile = (ROOT / 'Makefile').read_text()
        self.assertIn('yandex-archive:', makefile)
        self.assertIn('cloud-package-check:', makefile)
        self.assertIn('-m scripts.check_cloud_package', makefile)


if __name__ == '__main__':
    unittest.main()

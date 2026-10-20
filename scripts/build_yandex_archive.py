"""Build the stdlib-only, explicit once payload without credentials or local configs."""

import argparse
import ast
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import zipfile

from zontcontrol.algorithms.boiler_reset import load_config as load_reset_config
from zontcontrol.autonomy.catalog import BOILER_RESET_SPEC
from zontcontrol.autonomy.contracts import Mode, validate_catalog
from zontcontrol.config import ConfigurationError, load_controller_config, load_registry_config


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    'handler.py',
    'zontcontrol/__init__.py',
    'zontcontrol/application.py',
    'zontcontrol/cloud.py',
    'zontcontrol/config.py',
    'zontcontrol/logging_config.py',
    'zontcontrol/anomaly.py',
    'zontcontrol/detection_profile.py',
    'zontcontrol/heating.py',
    'zontcontrol/heating_adapter.py',
    'zontcontrol/monitor.py',
    'zontcontrol/commands.py',
    'zontcontrol/history.py',
    'zontcontrol/inventory.py',
    'zontcontrol/algorithms/__init__.py',
    'zontcontrol/algorithms/boiler_reset.py',
    'zontcontrol/autonomy/__init__.py',
    'zontcontrol/autonomy/catalog.py',
    'zontcontrol/autonomy/contracts.py',
    'zontcontrol/autonomy/registry.py',
    'zontcontrol/autonomy/runtime.py',
    'zontcontrol/controller/__init__.py',
    'zontcontrol/controller/auth.py',
    'zontcontrol/controller/contracts.py',
    'zontcontrol/controller/service.py',
    'zontcontrol/controller/transport.py',
    'zontcontrol/controller/zont.py',
)
CONFIG_FILES = ('controller.json', 'registry.json', 'boiler_reset.json')
MANIFEST = 'build-manifest.json'
MEMBERS = frozenset(SOURCE_FILES) | {f'config/{name}' for name in CONFIG_FILES} | {MANIFEST}
MAX_BYTES = 10 * 1024 * 1024
SHA_PATTERN = re.compile(r'[0-9a-f]{40}')


class ArchiveError(ValueError):
    """A fixed diagnostic; never echo input values, paths or subprocess output."""


def require(condition, reason):
    if not condition:
        raise ArchiveError(reason)


def _no_symlinks(path):
    return not any(item.is_symlink() for item in (path, *path.parents))


def _read_input(root, name):
    path = root / name
    require(_no_symlinks(path) and path.is_file(), 'Missing or unsafe allowlisted input')
    return path.read_bytes()


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _object(pairs):
    data = {}
    for key, value in pairs:
        require(key not in data, 'Invalid JSON object')
        data[key] = value
    return data


def _reject_constant(_value):
    raise ArchiveError('Invalid JSON number')


def _finite_float(value):
    result = float(value)
    require(math.isfinite(result), 'Invalid JSON number')
    return result


def _read_json(data):
    try:
        value = json.loads(data, object_pairs_hook=_object, parse_constant=_reject_constant,
                           parse_float=_finite_float)
    except (ValueError, UnicodeError):
        raise ArchiveError('Invalid JSON object') from None
    require(isinstance(value, dict), 'Invalid JSON object')
    return value


def _application_version(data):
    try:
        tree = ast.parse(data)
        values = [ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and any(
                      isinstance(target, ast.Name) and target.id == '__version__'
                      for target in node.targets)]
        require(len(values) == 1 and isinstance(values[0], str)
                and re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',
                                 values[0]) is not None, 'Invalid application version')
        return values[0]
    except (SyntaxError, ValueError, TypeError):
        raise ArchiveError('Invalid application version') from None


def validate_configs(entries):
    """Use the runtime's validators with a reader limited to in-memory ZIP configs."""
    data = {name: _read_json(entries[f'config/{name}']) for name in CONFIG_FILES}
    def reader(path):
        return data[path.name]
    try:
        load_controller_config(reader=reader)
        load_reset_config(reader=reader)
        registry = load_registry_config(reader=reader)
        spec = replace(BOILER_RESET_SPEC, modes=frozenset({Mode.ONCE}))
        require(validate_catalog((spec,), registry) == (spec,), 'Cloud once module is disabled')
        registry.timeout_for(spec).resolve(300)
    except (ConfigurationError, KeyError, TypeError, ValueError, OverflowError):
        raise ArchiveError('Invalid cloud configuration') from None


def prepare_entries(root, *, contact=None):
    """Snapshot only approved input bytes; no application or transport is created."""
    root = Path(root).absolute()
    contact = os.environ.get('ZONT_CONTACT') if contact is None else contact
    require(isinstance(contact, str) and bool(contact.strip()), 'Set nonempty ZONT_CONTACT')
    try:
        entries = {name: _read_input(root, name) for name in SOURCE_FILES}
        for name in CONFIG_FILES:
            data = _read_json(_read_input(root, f'deploy/cloud-config/{name}'))
            if name == 'controller.json':
                require('contact' in data, 'Missing cloud contact field')
                data['contact'] = contact
            entries[f'config/{name}'] = _json_bytes(data)
        validate_configs(entries)
        return entries
    except OSError:
        raise ArchiveError('Cloud inputs unavailable') from None


def source_identity(root, expected_sha=None):
    expected_sha = os.environ.get('CLOUD_PACKAGE_SHA', '') if expected_sha is None else expected_sha
    require(not expected_sha or isinstance(expected_sha, str)
            and SHA_PATTERN.fullmatch(expected_sha), 'Invalid CLOUD_PACKAGE_SHA')
    try:
        sha_result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True,
                                    text=True, timeout=10, check=False)
        status = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=normal'],
                                cwd=root, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ArchiveError('Git source identity unavailable') from None
    sha = sha_result.stdout.strip()
    require(sha_result.returncode == status.returncode == 0 and SHA_PATTERN.fullmatch(sha),
            'Git source identity unavailable')
    require(not expected_sha or expected_sha == sha, 'Cloud package source SHA mismatch')
    return sha, bool(status.stdout)


def file_hashes(entries):
    return {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()
            if name != MANIFEST}


def read_archive(path):
    """Check exact names, file types, bounds and CRC before exposing member bytes."""
    path = Path(path).absolute()
    try:
        require(_no_symlinks(path) and path.is_file() and path.stat().st_size <= MAX_BYTES,
                'Missing or unsafe cloud ZIP')
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            require(len(names) == len(MEMBERS) and set(names) == MEMBERS,
                    'Cloud ZIP membership mismatch')
            require(sum(member.file_size for member in members) <= MAX_BYTES
                    and all(member.filename == member.orig_filename
                            and not member.is_dir() and not member.flag_bits & 1
                            and stat.S_IFMT(member.external_attr >> 16) in (0, stat.S_IFREG)
                            for member in members), 'Unsafe cloud ZIP member')
            return {member.filename: archive.read(member) for member in members}
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError,
            UnicodeError, ValueError) as error:
        if isinstance(error, ArchiveError):
            raise
        raise ArchiveError('Invalid cloud ZIP or CRC') from None


def verify_entries(entries, *, expected_sha=None, require_clean=False, expected_entries=None):
    """Validate one already read ZIP snapshot without reopening its path."""
    require(set(entries) == MEMBERS, 'Cloud ZIP membership mismatch')
    manifest = _read_json(entries[MANIFEST])
    require(set(manifest) == {'schema_version', 'app_version', 'source_sha', 'dirty', 'files'}
            and type(manifest['schema_version']) is int and manifest['schema_version'] == 1
            and type(manifest['dirty']) is bool
            and isinstance(manifest['source_sha'], str)
            and SHA_PATTERN.fullmatch(manifest['source_sha'])
            and manifest['app_version'] == _application_version(entries['zontcontrol/__init__.py'])
            and manifest['files'] == file_hashes(entries), 'Cloud manifest drift')
    require(not expected_sha or manifest['source_sha'] == expected_sha,
            'Cloud package source SHA mismatch')
    require(not require_clean or not manifest['dirty'], 'Dirty cloud package cannot deploy')
    require(expected_entries is None or entries == expected_entries, 'Cloud ZIP byte drift')
    validate_configs(entries)
    return manifest


def verify_archive(path, *, expected_sha=None, require_clean=False, expected_entries=None):
    """Validate a ready artifact; deployment can require its exact SHA and clean source."""
    return verify_entries(read_archive(path), expected_sha=expected_sha, require_clean=require_clean,
                          expected_entries=expected_entries)


def _write_archive(path, entries):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def build_archive(project_root=ROOT, output_path=None, *, contact=None, expected_sha=None):
    """Verify a staged ZIP completely, then replace the destination atomically."""
    root = Path(project_root).absolute()
    output = Path(output_path).absolute() if output_path is not None else root / 'build/yandex-function.zip'
    require(_no_symlinks(root) and _no_symlinks(output), 'Unsafe cloud build path')
    identity = source_identity(root, expected_sha)
    entries = prepare_entries(root, contact=contact)
    require(source_identity(root, expected_sha) == identity,
            'Source identity changed while reading cloud inputs')
    entries[MANIFEST] = _json_bytes({'schema_version': 1,
        'app_version': _application_version(entries['zontcontrol/__init__.py']),
        'source_sha': identity[0], 'dirty': identity[1], 'files': file_hashes(entries)})
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.yandex-archive-', dir=output.parent) as directory:
            staged = Path(directory) / 'function.zip'
            _write_archive(staged, entries)
            verify_archive(staged, expected_sha=identity[0], expected_entries=entries)
            # Standard build/ is ignored; arbitrary output destinations may be untracked.
            require(source_identity(root, expected_sha)[0] == identity[0],
                    'Source HEAD changed during cloud build')
            staged.replace(output)
    except OSError:
        raise ArchiveError('Cloud archive output unavailable') from None
    return output


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(arguments)
    try:
        path = build_archive(args.project_root, args.output)
        manifest = verify_archive(path)
        print(json.dumps({'status': 'ok', 'app_version': manifest['app_version'],
                          'source_sha': manifest['source_sha'], 'dirty': manifest['dirty']}))
        return 0
    except ArchiveError as error:
        print(f'Cloud archive failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

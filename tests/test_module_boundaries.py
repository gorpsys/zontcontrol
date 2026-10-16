import ast
import importlib.util
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BoundaryTests(unittest.TestCase):
    def test_removed_prototype_modules_and_entrypoint(self):
        for module in ('client', 'cli', 'control'):
            self.assertIsNone(importlib.util.find_spec('zontcontrol.' + module))
        scripts = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['scripts']
        self.assertEqual(scripts['zontcontrol'], 'zontcontrol.application:main')
        self.assertNotIn('zontcontrol-control', scripts)

    def test_no_prototype_imports_symbols_or_environment_in_algorithms(self):
        for directory in ('zontcontrol', 'scripts'):
            for path in (ROOT / directory).rglob('*.py'):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Name):
                        self.assertNotIn(node.id, {'ZontClient', 'apply_command'}, str(path))
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotIn(node.module, {'zontcontrol.client', 'zontcontrol.cli',
                                                      'zontcontrol.control'}, str(path))
        for path in (ROOT / 'zontcontrol/algorithms').glob('*.py'):
            self.assertNotIn('environ', path.read_text())


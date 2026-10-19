import ast
import importlib.util
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BoundaryTests(unittest.TestCase):
    def test_production_consumers_use_static_coordination_surface(self):
        for path in (ROOT / 'zontcontrol').rglob('*.py'):
            relative = path.relative_to(ROOT).with_suffix('')
            if relative.parts[1] == 'coordination':
                continue
            package = '.'.join(relative.parts[:-1])
            for node in ast.walk(ast.parse(path.read_text())):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = '.' * node.level + (node.module or '')
                    module = importlib.util.resolve_name(module, package)
                    modules = [module]
                    if module == 'zontcontrol.coordination':
                        from zontcontrol.coordination import __all__
                        self.assertTrue(all(alias.name in __all__ for alias in node.names),
                                        f'{path}:{node.lineno}')
                for module in modules:
                    self.assertFalse(module.startswith('zontcontrol.coordination.'),
                                     f'{path}:{node.lineno}: {module}')

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

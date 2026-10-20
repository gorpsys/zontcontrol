import ast
import importlib.util
from pathlib import Path
import sys
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BoundaryTests(unittest.TestCase):
    def test_heating_model_imports_only_stdlib(self):
        tree = ast.parse((ROOT / 'zontcontrol/heating.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split('.')[0], sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIn((node.module or '').split('.')[0], sys.stdlib_module_names)

    def test_heating_adapter_does_not_depend_on_application_or_config(self):
        tree = ast.parse((ROOT / 'zontcontrol/heating_adapter.py').read_text())
        for node in ast.walk(tree):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module
                     else [alias.name for alias in node.names] if isinstance(node, ast.ImportFrom)
                     else [])
            for name in names:
                self.assertFalse({'algorithms', 'application', 'config', 'json'}
                                 & set(name.split('.')), name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, {'isoformat', 'fromisoformat'})

    def test_monitor_does_not_depend_on_controller_algorithms_or_scripts(self):
        tree = ast.parse((ROOT / 'zontcontrol/monitor.py').read_text())
        for node in ast.walk(tree):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module
                     else [alias.name for alias in node.names] if isinstance(node, ast.ImportFrom)
                     else [])
            for name in names:
                self.assertFalse({'controller', 'algorithms', 'scripts'} & set(name.split('.')), name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, 'fromisoformat')
        source = (ROOT / 'zontcontrol/monitor.py').read_text()
        for name in ('temperature_sensors', 'circuits', 'web_elements'):
            self.assertNotIn(name, source)

    def test_no_control_synchronization_components_or_imports(self):
        for path in (ROOT/'zontcontrol').rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ''
                    self.assertFalse(any(part in module.split('.') for part in
                        ('coordination', 'ownership', 'requests', 'control_types')), str(path))
        self.assertFalse((ROOT/'zontcontrol/coordination').exists())
        for name in ('ownership.py', 'requests.py'):
            self.assertFalse((ROOT/'zontcontrol/controller'/name).exists())

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

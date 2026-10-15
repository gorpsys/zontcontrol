import tempfile
import unittest
from pathlib import Path

from scripts.check_project import check, check_links


class DocumentationLinkTests(unittest.TestCase):
    def test_missing_file_and_heading_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "README.md"
            target = root / "guide.md"
            target.write_text("# Начало работы\n")
            text = "[valid](guide.md#начало-работы) [file](missing.md) [heading](guide.md#missing)"
            self.assertEqual(check_links(source, text, root), [
                "missing local link: missing.md", "missing heading: guide.md#missing"])


class ReleaseMetadataTests(unittest.TestCase):
    def test_lint_rejects_mismatch_and_old_tag_derived_metadata_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "zontcontrol").mkdir()
            (root / "zontcontrol/__init__.py").write_text('__version__ = "0.1.0"\n')
            (root / "CHANGELOG.md").write_text("## [0.1.0](CHANGELOG.md) - 2026-10-15\n- Change.\n")
            metadata = ('[project]\ndynamic = ["version"]\ndependencies = []\n'
                        '[tool.setuptools.dynamic]\nversion = {attr = "zontcontrol.__version__"}\n')
            (root / "pyproject.toml").write_text(metadata)
            self.assertEqual(check(root)[1], [])
            (root / "zontcontrol/__init__.py").write_text('__version__ = "0.1.1"\n')
            self.assertIn("application version and first CHANGELOG release must match", check(root)[1])
            (root / "pyproject.toml").write_text(metadata.replace("zontcontrol.__version__",
                                                                  "zontcontrol._version.__version__"))
            self.assertIn("pyproject.toml: version must come from zontcontrol.__version__", check(root)[1])


if __name__ == "__main__":
    unittest.main()

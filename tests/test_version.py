"""The application, release notes, package and CLI share one declared version."""

import contextlib
import io
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from scripts.release_version import application_version, parse_version, read_release
from zontcontrol import __version__
from zontcontrol.cli import main as read_main
from zontcontrol.control import main as control_main
from zontcontrol.get_token import main as token_main


class VersionTests(unittest.TestCase):
    def test_package_metadata_and_changelog_use_the_declared_version(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(application_version(root), parse_version(__version__))
        release = read_release((root / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertEqual(release.name, __version__)
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["tool"]["setuptools"]["dynamic"]["version"]["attr"],
                         "zontcontrol.__version__")

    def test_version_commands_do_not_prompt_or_create_api_client(self):
        for name, main in (("zontcontrol.cli", read_main), ("zontcontrol.control", control_main),
                           ("zontcontrol.get_token", token_main)):
            with self.subTest(name=name), patch("sys.argv", [name, "--version"]), \
                    patch(f"{name}.ZontClient") as client, patch("builtins.input") as prompt, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as caught:
                    main()
                self.assertEqual(caught.exception.code, 0)
                self.assertEqual(output.getvalue().strip(), "zontcontrol " + __version__)
                client.assert_not_called()
                prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()

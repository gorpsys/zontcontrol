"""Release validation and publication against disposable local repositories."""

import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.release_version import ReleaseError, main, parse_version, publish, read_release, validate


def changelog(version, notes="- A documented change.", release_date="2026-10-15"):
    return f"# Changes\n\n## Не выпущено\n\n## [{version}](release.md) - {release_date}\n\n{notes}\n"


class ReleaseDocumentTests(unittest.TestCase):
    def test_versions_compare_numerically_and_reject_ambiguous_values(self):
        self.assertGreater(parse_version("1.10.0"), parse_version("1.9.9"))
        for value in ("v1.2.3", "01.2.3", "1.2", "1.2.3.dev1", "1.2.3\n"):
            with self.subTest(value=value), self.assertRaises(ReleaseError):
                parse_version(value)

    def test_release_notes_ignore_fenced_examples(self):
        example = "```markdown\n" + changelog("9.9.9") + "```\n"
        release = read_release(example + changelog("0.1.0"))
        self.assertEqual(release.name, "0.1.0")
        self.assertEqual(release.notes, "- A documented change.")

    def test_invalid_or_missing_release_cannot_pass(self):
        for document in ("# Changes\n## Не выпущено\n", changelog("1.0.0", ""),
                         changelog("1.0.0", release_date="2026-02-30"),
                         "## 1.0.0 - 2026-10-15\n- Change.\n"):
            with self.subTest(document=document), self.assertRaises(ReleaseError):
                read_release(document)
        self.assertIsNone(read_release("# Changes\n## Не выпущено\n", required=False))


@unittest.skipUnless(shutil.which("git"), "git is required for release integration tests")
class ReleaseRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "source"
        self.root.mkdir()
        self.git("init", "--quiet", "--initial-branch=master")
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release@example.invalid")
        remote = Path(self.directory.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(remote)],
                       check=True, capture_output=True, timeout=10)
        self.git("remote", "add", "origin", str(remote))
        self.initial = self.commit_release("0.1.0")

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], cwd=self.root, text=True,
                              capture_output=True, check=True, timeout=10).stdout.strip()

    def commit_release(self, version, *, app_version=None):
        package = self.root / "zontcontrol"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text(
            f'__version__ = "{app_version or version}"\n', encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text(changelog(version), encoding="utf-8")
        self.git("add", "CHANGELOG.md", "zontcontrol/__init__.py")
        self.git("commit", "--quiet", "-m", "release " + version)
        return self.git("rev-parse", "HEAD")

    def test_first_release_does_not_require_an_existing_tag(self):
        release, sha, exists = validate(self.root)
        self.assertEqual((release.name, sha, exists), ("0.1.0", self.initial, False))

    def test_application_is_read_without_importing_it(self):
        path = self.root / "zontcontrol/__init__.py"
        path.write_text('__version__ = "0.1.0"\nraise RuntimeError("do not import")\n')
        self.git("add", "zontcontrol/__init__.py")
        self.git("commit", "--quiet", "-m", "side effect must not run")
        self.assertEqual(validate(self.root)[0].name, "0.1.0")

    def test_application_and_changelog_must_agree(self):
        self.commit_release("0.2.0", app_version="0.1.1")
        with self.assertRaisesRegex(ReleaseError, "match"):
            validate(self.root)

    def test_pr_must_advance_base_even_when_tag_has_not_been_created(self):
        self.git("commit", "--quiet", "--allow-empty", "-m", "no release bump")
        with self.assertRaisesRegex(ReleaseError, "greater"):
            validate(self.root, base=self.initial)
        self.commit_release("0.1.1")
        self.assertEqual(validate(self.root, base=self.initial)[0].name, "0.1.1")

    def test_full_base_sha_is_required(self):
        for base in ("master", "HEAD", self.initial[:7], "--help"):
            with self.subTest(base=base), self.assertRaises(ReleaseError):
                validate(self.root, base=base)

    def test_existing_versions_compare_as_numbers(self):
        self.git("tag", "-a", "v1.9.0", "-m", "prior version")
        self.commit_release("1.10.0")
        self.assertEqual(validate(self.root)[0].name, "1.10.0")
        self.commit_release("1.8.0")
        with self.assertRaises(ReleaseError):
            validate(self.root)

    def test_publication_creates_annotated_tag_and_is_idempotent(self):
        publish(self.root)
        tag_object = self.git("rev-parse", "refs/tags/v0.1.0")
        self.assertEqual(self.git("cat-file", "-t", "v0.1.0"), "tag")
        self.assertEqual(self.git("rev-parse", "v0.1.0^{commit}"), self.initial)
        self.assertIn("A documented change.", self.git("tag", "-l", "v0.1.0", "--format=%(contents)"))
        publish(self.root)
        self.assertEqual(self.git("rev-parse", "v0.1.0"), tag_object)
        self.assertIn(tag_object, self.git("ls-remote", "--tags", "origin"))

    def test_tag_on_another_commit_and_lightweight_tag_are_rejected(self):
        self.git("tag", "-a", "v0.1.0", "-m", "published")
        self.git("commit", "--quiet", "--allow-empty", "-m", "different commit")
        with self.assertRaisesRegex(ReleaseError, "different commit"):
            validate(self.root, allow_existing=True)
        self.git("tag", "v0.1.1")
        self.commit_release("0.1.1")
        self.git("tag", "-d", "v0.1.1")
        self.git("tag", "v0.1.1")
        with self.assertRaisesRegex(ReleaseError, "annotated"):
            validate(self.root, allow_existing=True)

    def test_delayed_merge_job_keeps_its_original_release(self):
        publish(self.root)
        earlier = self.commit_release("0.1.1")
        self.commit_release("0.1.2")
        publish(self.root)
        self.git("checkout", "--quiet", "--detach", earlier)
        publish(self.root)
        self.assertEqual(self.git("rev-parse", "v0.1.1^{commit}"), earlier)
        self.assertIn("refs/tags/v0.1.2", self.git("ls-remote", "--tags", "origin"))

    def test_conflicting_remote_tag_is_never_overwritten(self):
        self.git("tag", "-a", "v0.1.1", "-m", "conflicting tag")
        original = self.git("rev-parse", "v0.1.1")
        self.git("push", "--quiet", "origin", "refs/tags/v0.1.1")
        self.git("tag", "-d", "v0.1.1")
        self.commit_release("0.1.1")
        with self.assertRaises(ReleaseError):
            publish(self.root)
        self.assertIn(original, self.git("ls-remote", "--tags", "origin", "refs/tags/v0.1.1"))

    def test_uncommitted_release_files_cannot_be_tagged(self):
        (self.root / "CHANGELOG.md").write_text(changelog("0.1.1"))
        (self.root / "zontcontrol/__init__.py").write_text('__version__ = "0.1.1"\n')
        with self.assertRaisesRegex(ReleaseError, "commit"):
            publish(self.root)
        self.assertEqual(self.git("tag", "--list"), "")

    def test_check_does_not_create_tags_and_errors_do_not_expose_git_output(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["check", "--project-root", str(self.root)]), 0)
        self.assertEqual(self.git("tag", "--list"), "")
        failure = subprocess.CalledProcessError(1, ["git"], stderr="secret-value")
        with patch("scripts.release_version.subprocess.run", side_effect=failure), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["check", "--project-root", str(self.root)]), 1)
        self.assertNotIn("secret-value", errors.getvalue())


if __name__ == "__main__":
    unittest.main()

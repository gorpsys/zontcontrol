"""Check a declared release and publish its immutable annotated Git tag."""

import argparse
import ast
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
import subprocess
import sys


VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


class ReleaseError(ValueError):
    """The release cannot be validated or published safely."""


@dataclass(frozen=True)
class Release:
    version: tuple[int, int, int]
    date: str
    notes: str

    @property
    def name(self) -> str:
        return ".".join(map(str, self.version))


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(VERSION, value)
    if match is None:
        raise ReleaseError("version must be X.Y.Z without leading zeros or suffixes")
    return tuple(map(int, match.groups()))


def read_release(document: str, *, required: bool = True) -> Release | None:
    # Code examples must never supply a release that the author did not declare.
    document = re.sub(r"(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*$", "", document)
    sections = re.split(r"^## (.+)$", document, flags=re.MULTILINE)
    for title, body in zip(sections[1::2], sections[2::2]):
        if title.strip() == "Не выпущено":
            continue
        heading = re.fullmatch(r"\[([^\]]+)\]\([^)]+\) - ([0-9]{4}-[0-9]{2}-[0-9]{2})", title)
        if heading is None:
            raise ReleaseError("first release must use ## [X.Y.Z](link) - YYYY-MM-DD")
        version = parse_version(heading[1])
        try:
            date.fromisoformat(heading[2])
        except ValueError as error:
            raise ReleaseError("invalid release date") from error
        if not any(line.startswith("- ") and line[2:].strip() for line in body.splitlines()):
            raise ReleaseError("release notes must contain at least one change")
        return Release(version, heading[2], body.strip())
    if required:
        raise ReleaseError("declare a numbered release in CHANGELOG.md")
    return None


def application_version(root: Path) -> tuple[int, int, int]:
    """Read metadata without importing the application, credentials or transport."""
    tree = ast.parse((root / "zontcontrol/__init__.py").read_text(encoding="utf-8"))
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "__version__"
                      for target in node.targets)]
    if (len(values) != 1 or not isinstance(values[0], ast.Constant)
            or not isinstance(values[0].value, str)):
        raise ReleaseError("zontcontrol.__version__ must be one literal X.Y.Z string")
    return parse_version(values[0].value)


def git(root: Path, *arguments: str, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(["git", *arguments], cwd=root, input=input_text,
                                text=True, capture_output=True, check=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        # Provider output may contain a remote URL or credentials.
        raise ReleaseError("git command failed: " + arguments[0]) from error
    return result.stdout.strip()


def base_version(root: Path, base: str) -> tuple[int, int, int] | None:
    if re.fullmatch(r"[0-9a-f]{40}", base) is None:
        raise ReleaseError("base must be a full commit SHA")
    git(root, "rev-parse", "--verify", base + "^{commit}")
    if not git(root, "ls-tree", "--name-only", base, "--", "CHANGELOG.md"):
        return None
    release = read_release(git(root, "show", base + ":CHANGELOG.md"), required=False)
    return None if release is None else release.version


def tag_versions(root: Path, tags: list[str], sha: str, *,
                 allow_descendants: bool) -> list[tuple[int, int, int]]:
    versions = []
    for tag in tags:
        if not tag.startswith("v") or re.fullmatch(VERSION, tag[1:]) is None:
            continue
        if allow_descendants:
            target = git(root, "rev-parse", tag + "^{commit}")
            if target != sha and git(root, "merge-base", sha, target) == sha:
                # A later merge can finish its tag job before an earlier job retries.
                continue
        versions.append(parse_version(tag[1:]))
    return versions


def validate(root: Path, *, base: str | None = None,
             allow_existing: bool = False) -> tuple[Release, str, bool]:
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ReleaseError("project root must be the Git repository root")
    if git(root, "status", "--porcelain", "--untracked-files=normal", "--",
           "CHANGELOG.md", "zontcontrol/__init__.py"):
        raise ReleaseError("commit release files before checking or publishing a release")
    candidate = read_release((root / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert candidate is not None
    if application_version(root) != candidate.version:
        raise ReleaseError("application version and first CHANGELOG release must match")
    sha = git(root, "rev-parse", "HEAD")
    tags = git(root, "for-each-ref", "--format=%(refname:strip=2)", "refs/tags").splitlines()
    name = "v" + candidate.name
    if allow_existing and name in tags:
        if git(root, "rev-parse", name + "^{commit}") != sha:
            raise ReleaseError("version tag already belongs to a different commit")
        if git(root, "cat-file", "-t", "refs/tags/" + name) != "tag":
            raise ReleaseError("release tag must be annotated")
        return candidate, sha, True
    versions = tag_versions(root, tags, sha, allow_descendants=allow_existing)
    previous = base_version(root, base) if base else None
    if previous is not None:
        versions.append(previous)
    if versions and candidate.version <= max(versions):
        raise ReleaseError("release version must be greater than existing tags and base version")
    return candidate, sha, False


def publish(root: Path) -> Release:
    parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    candidate, sha, exists = validate(root, base=parents[0] if parents else None,
                                       allow_existing=True)
    if not exists:
        message = f"zontcontrol {candidate.name}\n\n{candidate.date}\n\n{candidate.notes}\n"
        git(root, "-c", "user.name=github-actions[bot]", "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "tag", "-a", "v" + candidate.name, sha, "-F", "-", input_text=message)
    # No force, deletion or movement of a conflicting remote tag.
    git(root, "push", "origin", "refs/tags/v" + candidate.name)
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Validate a committed candidate without creating tags")
    check.add_argument("--base", help="Full SHA of the PR base")
    check.add_argument("--allow-existing", action="store_true")
    tag = commands.add_parser("tag", help="Publish the release tag; intended for the master workflow")
    for command in (check, tag):
        command.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        if args.command == "tag":
            candidate = publish(args.project_root)
        else:
            candidate, _, _ = validate(args.project_root, base=args.base,
                                       allow_existing=args.allow_existing)
        print(f"Release {candidate.name}: {args.command} OK")
        return 0
    except (ReleaseError, OSError, SyntaxError) as error:
        reason = str(error) if isinstance(error, ReleaseError) else "release files unavailable or invalid"
        print(f"Release rejected: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

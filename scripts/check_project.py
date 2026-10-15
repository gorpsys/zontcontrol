"""Offline syntax, documentation link, whitespace, and metadata checks."""

import ast
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .release_version import application_version, read_release


ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".md", ".toml", ".json", ".yml", ".yaml"}
SKIP_DIRECTORIES = {".git", ".idea", "__pycache__", ".venv", "build", "dist"}


def forbidden_name(name):
    return name == ".env" or name.startswith(".env.") or name == ".idea"


def project_files(root):
    for name in ("README.md", "CHANGELOG.md", "AGENTS.md", "pyproject.toml", "Makefile", ".gitignore"):
        path = root / name
        if path.is_file():
            yield path
    for name in ("zontcontrol", "scripts", "tests", "docs", "deploy", ".github"):
        for directory, subdirs, names in os.walk(root / name, followlinks=False):
            subdirs[:] = [d for d in subdirs if d not in SKIP_DIRECTORIES and not forbidden_name(d)
                          and not (Path(directory) / d).is_symlink()]
            for file in names:
                if forbidden_name(file):
                    continue
                path = Path(directory) / file
                if path.suffix in SUFFIXES and not path.is_symlink():
                    yield path


def anchors(text):
    result, seen = set(), {}
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if fenced or not line.startswith("#"):
            continue
        title = line.lstrip("# ").strip().lower()
        slug = re.sub(r"[^\w\- ]", "", title).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


def check_links(path, text, root):
    errors = []
    for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", text):
        link = match.group(1)
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc:
            continue
        target = path.parent / unquote(parsed.path) if parsed.path else path
        if any(forbidden_name(part) for part in target.parts):
            errors.append(f"restricted local link: {link}")
            continue
        target = target.resolve()
        if not target.is_relative_to(root):
            errors.append(f"local link outside project: {link}")
            continue
        if not target.is_file():
            errors.append(f"missing local link: {link}")
        elif parsed.fragment and target.suffix == ".md":
            if unquote(parsed.fragment) not in anchors(target.read_text(encoding="utf-8")):
                errors.append(f"missing heading: {link}")
    return errors


def check(root=ROOT):
    errors = []
    files = list(project_files(root))
    for path in files:
        relative = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if line.rstrip() != line:
                errors.append(f"{relative}:{number}: trailing whitespace")
        try:
            if path.suffix == ".py":
                ast.parse(text, filename=str(relative))
            elif path.suffix == ".json":
                json.loads(text)
            elif path.suffix == ".toml":
                tomllib.loads(text)
        except (SyntaxError, ValueError) as error:
            errors.append(f"{relative}: {error}")
        if path.suffix == ".md":
            errors.extend(f"{relative}: {error}" for error in check_links(path, text, root))
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text())
        if project["project"].get("version") or "version" not in project["project"].get("dynamic", []):
            errors.append("pyproject.toml: package version must use the declared application version")
        if project["tool"]["setuptools"]["dynamic"]["version"]["attr"] != "zontcontrol.__version__":
            errors.append("pyproject.toml: version must come from zontcontrol.__version__")
        release = read_release((root / "CHANGELOG.md").read_text(encoding="utf-8"))
        if release is not None and application_version(root) != release.version:
            errors.append("application version and first CHANGELOG release must match")
        if project["project"]["dependencies"]:
            errors.append("pyproject.toml: runtime dependencies need a documented contract change")
    except (KeyError, ValueError, OSError, SyntaxError) as error:
        errors.append(f"pyproject.toml: invalid package metadata: {error}")
    return files, errors


def main():
    files, errors = check()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Syntax, whitespace, local documentation links and metadata OK ({len(files)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

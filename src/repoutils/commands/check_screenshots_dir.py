"""Check that nothing but its own .gitignore is tracked in the screenshots directory.

A screenshot step writes PNGs into a directory whose .gitignore keeps them out
of Git. Deleting that directory (e.g. `rm -rf screenshots`, as a coding agent
might before a fresh run) and then `git add -A` would commit the PNGs along
with the .gitignore's deletion. So this fails unless Git's index lists exactly
that .gitignore in the directory, and both its staged and working tree copies
ignore everything but themselves: apart from comments and blank lines, they
must be exactly the lines `*` and `!/.gitignore`.

The directory is `[tool.repoutils.screenshots]`'s `dir` (default `screenshots`).
"""

import argparse
import subprocess
from pathlib import Path

from repoutils import config

DEFAULT_DIR = "screenshots"
RULES = ["*", "!/.gitignore"]


def screenshots_dir(root: Path = Path()) -> str:
    """The screenshots directory, relative to the repo root, from config."""
    value = config.section("screenshots", root).get("dir", DEFAULT_DIR)
    if not isinstance(value, str) or not value.strip("/"):
        raise config.ConfigError("[tool.repoutils.screenshots] dir must be a non-empty string")
    return value.strip("/")


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _ignores_all_but_itself(contents: bytes) -> bool:
    """Whether a .gitignore's rules, ignoring comments and blank lines, are exactly RULES."""
    # Git ignores trailing spaces, but a leading one is part of the pattern.
    lines = [line.rstrip() for line in contents.decode("utf-8", errors="replace").splitlines()]
    return [line for line in lines if line and not line.startswith("#")] == RULES


def _problems(directory: str = DEFAULT_DIR) -> list[str]:
    gitignore = f"{directory}/.gitignore"
    tracked = [path.decode() for path in _git("ls-files", "-z", "--", f"{directory}/").split(b"\0") if path]
    problems = [f"{path} is tracked; only {gitignore} should be" for path in tracked if path != gitignore]
    if gitignore not in tracked:
        problems.append(f"{gitignore} isn't tracked")
    elif not _ignores_all_but_itself(_git("show", f":{gitignore}")):
        problems.append(f"{gitignore} has unexpected staged contents (want the rules {' and '.join(RULES)})")
    worktree = Path(gitignore)
    if not worktree.is_file():
        problems.append(f"{gitignore} is missing from the working tree")
    elif not _ignores_all_but_itself(worktree.read_bytes()):
        problems.append(
            f"{gitignore} has unexpected contents in the working tree (want the rules {' and '.join(RULES)})"
        )
    return problems


def main(directory: str | None = None) -> int:
    directory = directory or screenshots_dir()
    problems = _problems(directory)
    if problems:
        print(f"Problems in {directory}/ (only its .gitignore belongs in Git):")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"Only {directory}/.gitignore is tracked under {directory}/, ignoring everything else.")
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="repoutils check-screenshots-dir", description=__doc__.splitlines()[0])
    parser.add_argument("--dir", help=f"the screenshots directory (default: from config, else {DEFAULT_DIR})")
    return main(parser.parse_args(argv).dir)

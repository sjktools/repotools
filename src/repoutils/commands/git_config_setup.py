"""Set the repo's local Git config settings the project relies on.

Idempotent, and prints (to stderr) only when it changes a setting, so a
justfile can run it on every recipe, e.g. with
``eager _ := shell('uv run repoutils git-config-setup')``. Outside a Git
checkout (e.g. a ``git archive`` copy) it does nothing, rather than failing.

Currently it sets only ``blame.ignoreRevsFile`` to ``.git-blame-ignore-revs``,
so ``git blame`` skips the commits listed there. Git 2.52 added the
``:(optional)`` prefix, so ``git blame`` doesn't fail on a checkout from before
the file existed, but 2.52.0's blame reads an uninitialised pointer when the
file is missing (fixed in 2.53.0), and older Git takes the prefix as part of
the file name. So it uses the prefix from Git 2.53, and with older Git sets the
plain name only if the file exists.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

IGNORE_REVS = ".git-blame-ignore-revs"
OPTIONAL_PREFIX_VERSION = (2, 53)


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def git_version() -> tuple[int, int]:
    """Git's major and minor version, or (0, 0) if they can't be read."""
    m = re.match(r"git version (\d+)\.(\d+)", _git("version").stdout)
    return (int(m[1]), int(m[2])) if m else (0, 0)


def ensure_setting(toplevel: Path, name: str, value: str) -> bool:
    """Set a local config setting unless it already has value; returns whether it changed."""
    current = _git("config", "--get", "--local", name, cwd=toplevel)
    if current.returncode not in (0, 1):
        raise RuntimeError(f"git config --get {name} failed:\n{current.stderr}")
    if current.stdout.rstrip("\n") == value:
        return False
    subprocess.run(["git", "config", "--local", name, value], cwd=toplevel, check=True)
    print(f"INFO: Updated local repo git config setting '{name}' to '{value}'", file=sys.stderr)
    return True


def main(cwd: Path = Path()) -> int:
    toplevel = _git("rev-parse", "--show-toplevel", cwd=cwd)
    if toplevel.returncode != 0:
        return 0
    root = Path(toplevel.stdout.strip())
    if git_version() >= OPTIONAL_PREFIX_VERSION:
        ensure_setting(root, "blame.ignoreRevsFile", f":(optional){IGNORE_REVS}")
    elif (root / IGNORE_REVS).is_file():
        ensure_setting(root, "blame.ignoreRevsFile", IGNORE_REVS)
    return 0


def run(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="repoutils git-config-setup", description=__doc__.splitlines()[0]).parse_args(argv)
    return main()

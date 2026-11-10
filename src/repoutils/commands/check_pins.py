"""Check that every workflow or local composite action step that installs uv
or just pins the version the project requires: pyproject.toml's [tool.uv]
required-version minimum and the justfile's minimum-version. A Dockerfile's
ghcr.io/astral-sh/uv stage, if there is one, must use that uv too."""

import argparse
import itertools
import re
import sys
import tomllib
from pathlib import Path

# (action, the `with:` input that pins its version)
UV = ("astral-sh/setup-uv", "version")
JUST = ("extractions/setup-just", "just-version")

_STEP_RE = re.compile(r"^(?P<indent>\s*)- ")
# A step's own `- ` marker, which YAML lets have more than one space.
_ITEM_RE = re.compile(r"^\s*-\s+")
_USES_RE = re.compile(r"^uses:\s*[\"']?(?P<action>[^@\s\"']+)@")
_WITH_RE = re.compile(r"^with:\s*(?:#.*)?$")
_JUST_MIN_RE = re.compile(r"^set minimum-version := '(?P<version>[^']+)'$", re.MULTILINE)
# The tag, without any @sha256: digest.
# A FROM line's flags (e.g. --platform=$BUILDPLATFORM) come before the image.
_DOCKER_UV_RE = re.compile(
    r"^FROM\s+(?:--\S+\s+)*ghcr\.io/astral-sh/uv:(?P<version>[^\s@]+)", re.MULTILINE | re.IGNORECASE
)


def steps(text: str) -> list[list[str]]:
    """The outermost YAML sequence items in a workflow (so each job step, but
    not a list nested in one), each as its lines: an item runs from its `- `
    line up to the next line (ignoring blanks and comments) indented no deeper
    than that `- `."""
    result: list[list[str]] = []
    current: list[str] | None = None
    indent = -1
    for line in text.splitlines():
        stripped = line.strip()
        if current is not None and (
            not stripped or stripped.startswith("#") or len(line) - len(line.lstrip()) > indent
        ):
            current.append(line)
        elif match := _STEP_RE.match(line):
            current = [line]
            indent = len(match["indent"])
            result.append(current)
        else:
            current = None
    return result


def inputs(step: list[str]) -> list[str]:
    """The lines of a step's `with:` mapping that start one of its keys, so
    that a key in another of the step's mappings (`env:`), or a line of a
    multi-line value (`sparse-checkout: |`), isn't taken for an input."""
    lines = [line for line in step if line.strip() and not line.strip().startswith("#")]
    for i, (indent, key) in enumerate(_keys(lines)):
        if _WITH_RE.match(key):
            mapping = list(itertools.takewhile(lambda body: _depth(body) > indent, lines[i + 1 :]))
            return [key for key in mapping if _depth(key) == _depth(mapping[0])]
    return []


def _keys(lines: list[str]) -> list[tuple[int, str]]:
    """Each line of a step (blanks and comments already dropped) with its
    indent and, where it starts one of the step's own keys, that key's text, or
    "" where it doesn't. The step's keys sit just past its `- `, so a line
    deeper in (in a `run: |` script, say) isn't taken for one of them."""
    if not lines:
        return []
    item = _ITEM_RE.match(lines[0])
    key_depth = item.end() if item else _depth(lines[0])
    keys = [(key_depth, lines[0][key_depth:])]
    keys += [(_depth(line), line.strip() if _depth(line) == key_depth else "") for line in lines[1:]]
    return keys


def _uses(step: list[str]) -> str | None:
    """The action a step uses, or None if it runs a script instead."""
    lines = [line for line in step if line.strip() and not line.strip().startswith("#")]
    for _, key in _keys(lines):
        if match := _USES_RE.match(key):
            return match["action"]
    return None


def _depth(line: str) -> int:
    return len(line) - len(line.lstrip())


def pins(text: str, action: str, key: str) -> list[str | None]:
    """The value of input key in each step of a workflow that uses action, or
    None where that step doesn't set it."""
    # The whole value, less its quotes or a trailing comment, so an unexpected
    # one (`${{ env.UV }}`, `latest`, `"0.13.6 # x"`) is reported as written.
    key_re = re.compile(
        rf"""^\s*{re.escape(key)}:\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<plain>.*?))\s*(?:\s#.*)?$"""
    )
    found: list[str | None] = []
    for step in steps(text):
        if _uses(step) == action:
            values = [
                next(v for v in m.groups() if v is not None) for line in inputs(step) if (m := key_re.match(line))
            ]
            found.append(values[0] if values else None)
    return found


def workflow_files(root: Path) -> list[Path]:
    """The workflows, and the local composite actions they use, whose steps the
    checks read: .github/workflows/*.y*ml and .github/actions/**/action.y*ml."""
    github = root / ".github"
    return sorted([*(github / "workflows").glob("*.y*ml"), *(github / "actions").glob("**/action.y*ml")])


def minimums(root: Path) -> dict[tuple[str, str], str]:
    with (root / "pyproject.toml").open("rb") as f:
        required = tomllib.load(f)["tool"]["uv"]["required-version"]
    if not required.startswith(">="):
        raise ValueError(f"[tool.uv] required-version {required!r} isn't of the form >=X.Y.Z")
    match = _JUST_MIN_RE.search((root / "justfile").read_text())
    if match is None:
        raise ValueError("justfile has no `set minimum-version := '...'`")
    # The lower bound of e.g. ">=0.13.6, <0.14".
    return {UV: required.removeprefix(">=").split(",", 1)[0].strip(), JUST: match["version"]}


def dockerfile_problems(root: Path, uv: str) -> list[str]:
    """Each Dockerfile uv stage whose tag isn't uv. A Dockerfile with no such
    stage is a problem too, as it more likely means _DOCKER_UV_RE no longer
    recognizes it; a project with no Dockerfile has nothing to check."""
    dockerfile = root / "Dockerfile"
    if not dockerfile.is_file():
        return []
    tags = [m["version"] for m in _DOCKER_UV_RE.finditer(dockerfile.read_text())]
    if not tags:
        return ["Dockerfile has no FROM ghcr.io/astral-sh/uv:<version> stage"]
    return [f"Dockerfile: FROM ghcr.io/astral-sh/uv:{tag}, not {uv}" for tag in tags if tag != uv]


def problems(root: Path) -> tuple[list[str], dict[tuple[str, str], int]]:
    """Each pin that's missing or differs from the minimum, and how many steps
    use each action. No step using one is a problem too, as it more likely
    means steps() or _USES_RE no longer recognize them than that CI stopped
    installing uv or just."""
    want = minimums(root)
    found: list[str] = []
    counts = dict.fromkeys(want, 0)
    for path in workflow_files(root):
        text = path.read_text()
        for (action, key), version in want.items():
            for pin in pins(text, action, key):
                counts[action, key] += 1
                if pin != version:
                    setting = f"{key}: {pin}" if pin else f"no {key}"
                    found.append(f"{path.relative_to(root).as_posix()}: a {action} step has {setting}, not {version}")
    found += [f"no workflow or action has a {action} step" for (action, _), count in counts.items() if not count]
    found += dockerfile_problems(root, want[UV])
    return found, counts


def main(root: Path = Path()) -> int:
    found, counts = problems(root)
    for problem in found:
        print(problem, file=sys.stderr)
    if found:
        return 1
    want = minimums(root)
    uv, just = f"uv {want[UV]} in {counts[UV]} steps", f"just {want[JUST]} in {counts[JUST]} steps"
    dockerfile = f", and the Dockerfile uv {want[UV]}" if (root / "Dockerfile").is_file() else ""
    print(f"workflows and actions pin {uv} and {just}{dockerfile}")
    return 0


def run(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="repoutils check-pins", description=__doc__.splitlines()[0]).parse_args(argv)
    return main()

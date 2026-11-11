"""Check that a repository has the issue and PR labels pyproject.toml defines,
or create and update them so it does.

Each label is a ``[[tool.repoutils.labels]]`` entry with a ``name`` and,
optionally, a ``color`` (six hex digits, with or without ``#``) and a
``description``. A setting a label leaves out is left as it is on GitHub.
Label names match regardless of case, as on GitHub.

``ensure`` creates every missing label, and fills in a description that's
empty on GitHub. A name whose case differs, a different color, or a different
non-empty description is only reported, unless ``--strict`` is given, when
``ensure`` changes it to match pyproject.toml. Labels that pyproject.toml
doesn't define are never changed or deleted.

``check`` fails exactly when ``ensure`` (with the same ``--strict``) would
change something, and reports the differences ``ensure`` would leave alone
without failing. Stdlib-only, like every repoutils command."""

import argparse
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoutils import config, github

TABLE = "[[tool.repoutils.labels]]"
# GitHub refuses a longer description.
MAX_DESCRIPTION = 100
# Labels per page of the list (GitHub's most).
PER_PAGE = 100
_COLOR_RE = re.compile(r"#?([0-9a-fA-F]{6})")


@dataclass(frozen=True)
class Label:
    name: str
    # Six lowercase hex digits without "#", as GitHub returns them; None to leave as is.
    color: str | None = None
    # None to leave as is.
    description: str | None = None


def load_labels(root: Path = Path()) -> list[Label]:
    """The labels ``root/pyproject.toml`` defines, raising ValueError if they're malformed."""
    entries = config.load(root).get("labels", [])
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        raise config.ConfigError(f"{TABLE} must be an array of tables")
    labels: list[Label] = []
    seen: dict[str, str] = {}
    for entry in entries:
        name, color, description = entry.get("name"), entry.get("color"), entry.get("description")
        if not isinstance(name, str) or not name.strip():
            raise config.ConfigError(f"every {TABLE} entry needs a non-empty name")
        where = f"{TABLE} {name!r}"
        unknown = sorted(set(entry) - {"name", "color", "description"})
        if unknown:
            raise config.ConfigError(f"{where} has unknown setting(s): {', '.join(unknown)}")
        if name.casefold() in seen:
            raise config.ConfigError(f"{where} is defined twice (label names ignore case)")
        seen[name.casefold()] = name
        if color is not None:
            match = _COLOR_RE.fullmatch(color) if isinstance(color, str) else None
            if not match:
                raise config.ConfigError(f'{where} color must be six hex digits, e.g. "d73a4a"')
            color = match.group(1).lower()
        if description is not None and not (isinstance(description, str) and len(description) <= MAX_DESCRIPTION):
            raise config.ConfigError(f"{where} description must be a string of at most {MAX_DESCRIPTION} characters")
        labels.append(Label(name, color, description))
    return labels


@dataclass
class Plan:
    """What ensure does for one label: create it, update some of its
    settings, and leave other differences alone (with a note on each)."""

    label: Label
    # The label's current name on GitHub, or None if it's missing.
    existing_name: str | None
    # The fields ensure sends to create or update the label; empty if none.
    fields: dict[str, str] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)
    left_alone: list[str] = field(default_factory=list)

    @property
    def needs_ensure(self) -> bool:
        return self.existing_name is None or bool(self.fields)


def plan(label: Label, existing: dict[str, Any] | None, *, strict: bool) -> Plan:
    """What ensure does to make existing (GitHub's label, None if it's missing) match label."""
    if existing is None:
        fields = {"name": label.name}
        if label.color is not None:
            fields["color"] = label.color
        if label.description is not None:
            fields["description"] = label.description
        return Plan(label, None, fields, ["missing"])
    result = Plan(label, existing["name"])

    def compare(setting: str, key: str, theirs: str, ours: str | None, *, update: bool) -> None:
        if ours is None or theirs == ours:
            return
        note = f"{setting} is {theirs!r} on GitHub, not {ours!r}"
        if update:
            result.fields[key] = ours
            result.changes.append(note)
        else:
            result.left_alone.append(note)

    compare("name", "new_name", existing["name"], label.name, update=strict)
    compare("color", "color", existing["color"].lower(), label.color, update=strict)
    description = existing.get("description") or ""
    compare("description", "description", description, label.description, update=strict or not description)
    return result


def plans(labels: Iterable[Label], existing: Iterable[dict[str, Any]], *, strict: bool) -> list[Plan]:
    by_name = {label["name"].casefold(): label for label in existing}
    return [plan(label, by_name.get(label.name.casefold()), strict=strict) for label in labels]


def repo_labels(repo: str, token: str) -> list[dict[str, Any]]:
    """Every label the repository has."""
    labels: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = github.request("GET", f"{github.API_URL}/repos/{repo}/labels?per_page={PER_PAGE}&page={page}", token)
        labels += batch
        if len(batch) < PER_PAGE:
            return labels
        page += 1


def apply(repo: str, token: str, p: Plan) -> None:
    """Creates or updates p's label, as p says."""
    if p.existing_name is None:
        github.request("POST", f"{github.API_URL}/repos/{repo}/labels", token, json_body=p.fields)
    elif p.fields:
        name = urllib.parse.quote(p.existing_name, safe="")
        github.request("PATCH", f"{github.API_URL}/repos/{repo}/labels/{name}", token, json_body=p.fields)


def _report(p: Plan, *, done: bool) -> list[str]:
    lines = []
    if p.existing_name is None:
        lines.append(f"{p.label.name}: {'created' if done else 'missing'}")
    elif p.changes:
        lines.append(f"{p.label.name}: {'updated' if done else 'needs updating'}: {'; '.join(p.changes)}")
    if p.left_alone:
        lines.append(f"{p.label.name}: left alone without --strict: {'; '.join(p.left_alone)}")
    return lines


def check(repo: str, token: str, labels: list[Label], *, strict: bool, exit_zero: bool) -> int:
    results = plans(labels, repo_labels(repo, token), strict=strict)
    for p in results:
        for line in _report(p, done=False):
            print(line)
    failing = sum(p.needs_ensure for p in results)
    if failing:
        print(f"{failing} of {len(labels)} label(s) in {repo} need `repoutils labels ensure`.")
        return 0 if exit_zero else 1
    print(
        f"All {len(labels)} label(s) in {repo} match pyproject.toml"
        + (" (except as noted)." if any(p.left_alone for p in results) else ".")
    )
    return 0


def ensure(repo: str, token: str, labels: list[Label], *, strict: bool) -> int:
    results = plans(labels, repo_labels(repo, token), strict=strict)
    for p in results:
        apply(repo, token, p)
        for line in _report(p, done=True):
            print(line)
    changed = sum(p.needs_ensure for p in results)
    print(f"Created or updated {changed} of {len(labels)} label(s) in {repo}.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils labels",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="Reads labels with $GITHUB_TOKEN, defaulting to `gh auth token`; ensure needs issues: write.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,ensure}")
    check_parser = subparsers.add_parser(
        "check", help="fail if ensure would change anything", description="Fail if ensure would change anything."
    )
    ensure_parser = subparsers.add_parser(
        "ensure", help="create and update labels to match", description="Create and update labels to match."
    )
    for sub in (check_parser, ensure_parser):
        sub.add_argument(
            "--strict",
            action="store_true",
            help="also count (check) or change (ensure) a name's case, a color or a non-empty description "
            "that differs from pyproject.toml",
        )
        sub.add_argument(
            "--repo",
            default=os.environ.get("GITHUB_REPOSITORY"),
            help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
        )
    check_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="exit 0 even if labels need ensure (still exit 1 on an error, unlike `|| true`)",
    )
    args = parser.parse_args(argv)

    try:
        labels = load_labels()
        if not labels:
            print(f"No labels are defined: add {TABLE} entries to pyproject.toml.", file=sys.stderr)
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "check":
            return check(repo, token, labels, strict=args.strict, exit_zero=args.exit_zero)
        return ensure(repo, token, labels, strict=args.strict)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1


run = main

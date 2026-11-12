"""Check that a repository has the Actions and Dependabot secrets
pyproject.toml names, or print the names it has.

``[tool.repoutils.secrets]`` lists the names: ``actions`` for the repository's
Actions secrets, and ``dependabot`` for its Dependabot secrets. A list the
table leaves out isn't checked. Names match regardless of case, as on GitHub.

Only names are ever read or written: GitHub's API never returns a secret's
value, so there's no ``ensure`` to create one. Secrets are set with
``gh secret set`` (``--app dependabot`` for a Dependabot secret) or on the
repository's settings page.

``check`` fails if the repository lacks a secret the table names. With
``--fail-on-unexpected`` it also fails on secrets of a listed kind that the
table doesn't name (an empty list then means there must be none).

``dump`` prints the names of every secret the repository has as the table,
to paste into pyproject.toml (``check --fail-on-unexpected`` then passes).
Stdlib-only, like every repoutils command."""

import argparse
import os
import re
import subprocess
import sys
import urllib.error
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from repoutils import config, github

TABLE = "[tool.repoutils.secrets]"
# Secrets per page of the list (GitHub's most).
PER_PAGE = 100
# GitHub's rule for a secret's name: letters, digits and underscores, not
# starting with a digit, nor with GITHUB_ (which it reserves).
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Kind:
    # Its key in TABLE.
    key: str
    # The API path under /repos/{repo}/ that lists them.
    path: str
    # What GitHub calls them.
    title: str


KINDS = (Kind("actions", "actions/secrets", "Actions"), Kind("dependabot", "dependabot/secrets", "Dependabot"))


def load_secrets(root: Path = Path()) -> dict[str, list[str]]:
    """The secret names ``root/pyproject.toml`` lists, by kind, for each kind it lists;
    raising config.ConfigError (a ValueError) if they're malformed."""
    table = config.section("secrets", root)
    unknown = sorted(set(table) - {kind.key for kind in KINDS})
    if unknown:
        raise config.ConfigError(f"{TABLE} has unknown setting(s): {', '.join(unknown)}")
    result: dict[str, list[str]] = {}
    for kind in KINDS:
        if kind.key not in table:
            continue
        names = config.string_list(table, kind.key, [], TABLE)
        seen: set[str] = set()
        for name in names:
            where = f"{TABLE} {kind.key} {name!r}"
            if not _NAME_RE.fullmatch(name) or name.upper().startswith("GITHUB_"):
                raise config.ConfigError(
                    f"{where} isn't a valid secret name: use letters, digits and underscores, "
                    "not starting with a digit or GITHUB_"
                )
            if name.upper() in seen:
                raise config.ConfigError(f"{where} is listed twice (secret names ignore case)")
            seen.add(name.upper())
        result[kind.key] = names
    return result


def repo_secrets(repo: str, token: str, kind: Kind) -> list[str]:
    """The names of every secret of kind the repository has."""
    names: list[str] = []
    page = 1
    while True:
        batch = github.request(
            "GET", f"{github.API_URL}/repos/{repo}/{kind.path}?per_page={PER_PAGE}&page={page}", token
        )["secrets"]
        names += [secret["name"] for secret in batch]
        if len(batch) < PER_PAGE:
            return names
        page += 1


def missing_secrets(names: Iterable[str], existing: Iterable[str]) -> list[str]:
    """The names in names that existing lacks, ignoring case."""
    theirs = {name.upper() for name in existing}
    return [name for name in names if name.upper() not in theirs]


def dump_toml(existing: dict[str, list[str]]) -> str:
    """The secret names of each kind as TABLE, with every kind, sorted."""
    lines = [TABLE]
    for key, names in existing.items():
        if names:
            lines += [f"{key} = [", *(f"    {config.toml_string(name)}," for name in sorted(names)), "]"]
        else:
            lines.append(f"{key} = []")
    return "".join(f"{line}\n" for line in lines)


def dump(repo: str, token: str) -> int:
    print(dump_toml({kind.key: repo_secrets(repo, token, kind) for kind in KINDS}), end="")
    return 0


def check(repo: str, token: str, wanted: dict[str, list[str]], *, exit_zero: bool, fail_on_unexpected: bool) -> int:
    missing_count = unexpected_count = 0
    for kind in KINDS:
        if kind.key not in wanted:
            continue
        names = wanted[kind.key]
        existing = repo_secrets(repo, token, kind)
        missing = missing_secrets(names, existing)
        unexpected = missing_secrets(existing, names) if fail_on_unexpected else []
        for name in missing:
            print(f"{kind.title} secret {name}: missing")
        for name in unexpected:
            print(f"{kind.title} secret {name}: not listed in pyproject.toml")
        missing_count += len(missing)
        unexpected_count += len(unexpected)
    total = sum(len(names) for names in wanted.values())
    if missing_count:
        print(
            f"{missing_count} of {total} secret(s) are missing from {repo}: "
            "set them with `gh secret set` (`--app dependabot` for a Dependabot secret)."
        )
    if unexpected_count:
        print(
            f"{unexpected_count} secret(s) in {repo} aren't listed in pyproject.toml: "
            "add them there (`repoutils secrets dump` prints them) or delete them on GitHub."
        )
    if missing_count or unexpected_count:
        return 0 if exit_zero else 1
    print(
        f"All {total} secret(s) pyproject.toml lists exist in {repo}"
        + (", and it has no others of those kinds." if fail_on_unexpected else ".")
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils secrets",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="Reads secret names (never values) with $GITHUB_TOKEN, defaulting to `gh auth token`; "
        "it needs the Secrets and Dependabot secrets read permissions. "
        "Only repository secrets count, not organization or environment ones.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,dump}")
    check_parser = subparsers.add_parser(
        "check",
        help="fail if a secret pyproject.toml lists is missing",
        description="Fail if the repository lacks a secret pyproject.toml lists.",
    )
    dump_parser = subparsers.add_parser(
        "dump",
        help="print the repository's secret names as pyproject.toml settings",
        description=f"Print the name of every secret the repository has as {TABLE}, to paste into pyproject.toml.",
    )
    for sub in (check_parser, dump_parser):
        sub.add_argument(
            "--repo",
            default=os.environ.get("GITHUB_REPOSITORY"),
            help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
        )
    check_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="exit 0 even if secrets are missing (still exit 1 on an error, unlike `|| true`)",
    )
    check_parser.add_argument(
        "--fail-on-unexpected",
        action="store_true",
        help="also fail on secrets of a kind pyproject.toml lists that it doesn't name",
    )
    args = parser.parse_args(argv)

    try:
        # dump doesn't read pyproject.toml, so it works before there are any secrets there.
        wanted = {} if args.mode == "dump" else load_secrets()
        if not wanted and args.mode != "dump":
            print(
                f"No secrets are listed: add actions or dependabot to {TABLE} in pyproject.toml.",
                file=sys.stderr,
            )
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "dump":
            return dump(repo, token)
        return check(repo, token, wanted, exit_zero=args.exit_zero, fail_on_unexpected=args.fail_on_unexpected)
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

"""Check that a repository has the Actions, Dependabot and environment
secrets pyproject.toml names, or print the names it has.

``[tool.repoutils.secrets]`` lists the names: ``actions`` for the repository's
Actions secrets, ``dependabot`` for its Dependabot secrets, and
``environments.<name>`` for each deployment environment's secrets. A list the
table leaves out isn't checked. Names match regardless of case, as on GitHub.

Only names are ever read or written: GitHub's API never returns a secret's
value, so there's no ``ensure`` to create one. Secrets are set with
``gh secret set`` (``--app dependabot`` for a Dependabot secret, ``--env NAME``
for an environment's) or on the repository's settings page.

``check`` fails if the repository lacks a secret the table names, or an
environment it names. With ``--fail-on-unexpected`` it also fails on secrets of
a listed kind that the table doesn't name (an empty list then means there must
be none). If the table has ``environments``, that covers every environment's
secrets, so ``environments = {}`` means no environment may have any.

``dump`` prints the names of every secret the repository has as the table,
to paste into pyproject.toml (``check --fail-on-unexpected`` then passes).

``check-workflows`` needs no token: it fails if a workflow in
``.github/workflows`` uses a secret the table doesn't name, or the table names
one that no workflow uses. It also fails if a job uses a secret that only
environments hold without declaring one of them as its ``environment``.
``other`` lists the secrets workflows use that the repository doesn't hold
(such as an organization's), which ``check`` doesn't look for.
Stdlib-only, like every repoutils command."""

import argparse
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoutils import config, github
from repoutils.commands import environments as environments_command

TABLE = "[tool.repoutils.secrets]"
# Secrets per page of the list (GitHub's most).
PER_PAGE = 100
# GitHub's rule for a secret's name: letters, digits and underscores, not
# starting with a digit, nor with GITHUB_ (which it reserves).
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The key in TABLE for secrets that workflows use but the repository doesn't hold.
OTHER = "other"
# The key in TABLE for the sub-table of each environment's secrets.
ENVIRONMENTS = "environments"
WORKFLOWS = Path(".github") / "workflows"
# A workflow's use of a secret: secrets.NAME, secrets['NAME'] or secrets["NAME"].
_USE_RE = re.compile(r"\bsecrets\s*(?:\.|\[\s*['\"])\s*([A-Za-z_][A-Za-z0-9_]*)")
# The token GitHub gives every workflow run, which is no one's to set.
_BUILT_IN = "GITHUB_TOKEN"
# A workflow's top-level jobs key, and a key (such as a job's id) that starts a block.
_JOBS_RE = re.compile(r"jobs\s*:\s*")
_BLOCK_KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:(?:\s+(.*))?")
# A job's environment key, with its value if it's on the same line.
_ENVIRONMENT_RE = re.compile(r"environment\s*:\s*(.*)")
# The name in an environment mapping, block or flow.
_NAME_IN_MAPPING_RE = re.compile(r"(?:^|[{,])\s*name\s*:\s*([^,}]+)")
# A job's environment that only an expression gives, so it can't be known.
UNKNOWN = "${{ }}"


@dataclass(frozen=True)
class Kind:
    # Its key in TABLE.
    key: str
    # The API path under /repos/{repo}/ that lists them.
    path: str
    # What GitHub calls them.
    title: str


KINDS = (Kind("actions", "actions/secrets", "Actions"), Kind("dependabot", "dependabot/secrets", "Dependabot"))


@dataclass(frozen=True)
class Listed:
    """What ``[tool.repoutils.secrets]`` lists."""

    # The names of each kind it lists, by its key (``other`` among them).
    lists: dict[str, list[str]]
    # The names each environment holds, by its name, or None without ``environments``.
    environments: dict[str, list[str]] | None

    def all_names(self) -> list[str]:
        """Every name listed, in any list."""
        return [
            *(name for names in self.lists.values() for name in names),
            *(name for names in (self.environments or {}).values() for name in names),
        ]


def _names(table: dict[str, Any], key: str, where: str) -> list[str]:
    """``table[key]``, checked to be a list of distinct secret names."""
    names = config.string_list(table, key, [], where)
    seen: set[str] = set()
    for name in names:
        where_name = f"{where} {key} {name!r}"
        if not _NAME_RE.fullmatch(name) or name.upper().startswith("GITHUB_"):
            raise config.ConfigError(
                f"{where_name} isn't a valid secret name: use letters, digits and underscores, "
                "not starting with a digit or GITHUB_"
            )
        if name.upper() in seen:
            raise config.ConfigError(f"{where_name} is listed twice (secret names ignore case)")
        seen.add(name.upper())
    return names


def _load(root: Path) -> Listed:
    """What ``[tool.repoutils.secrets]`` in ``root/pyproject.toml`` lists."""
    table = config.section("secrets", root)
    keys = [*(kind.key for kind in KINDS), OTHER]
    unknown = sorted(set(table) - {*keys, ENVIRONMENTS})
    if unknown:
        raise config.ConfigError(f"{TABLE} has unknown setting(s): {', '.join(unknown)}")
    lists = {key: _names(table, key, TABLE) for key in keys if key in table}
    if ENVIRONMENTS not in table:
        return Listed(lists, None)
    where = f"{TABLE[:-1]}.{ENVIRONMENTS}]"
    by_environment = table[ENVIRONMENTS]
    if not isinstance(by_environment, dict):
        raise config.ConfigError(f"{TABLE} {ENVIRONMENTS} must be a table of each environment's secrets")
    environments: dict[str, list[str]] = {}
    seen: set[str] = set()
    for name in by_environment:
        if name.lower() in seen:
            raise config.ConfigError(f"{where} {config.toml_key(name)} is listed twice (environment names ignore case)")
        seen.add(name.lower())
        environments[name] = _names(by_environment, name, where)
    return Listed(lists, environments)


def load_secrets(root: Path = Path()) -> dict[str, list[str]]:
    """The repository secret names ``root/pyproject.toml`` lists, by kind, for
    each kind it lists; raising config.ConfigError (a ValueError) if they're malformed."""
    return {key: names for key, names in _load(root).lists.items() if key != OTHER}


def load_environment_secrets(root: Path = Path()) -> dict[str, list[str]] | None:
    """The secret names ``root/pyproject.toml`` lists for each environment, or
    None if it has no ``environments``; raising config.ConfigError if they're malformed."""
    return _load(root).environments


def _without_comment(line: str) -> str:
    """line up to its YAML or shell comment: a ``#`` that starts the line or
    follows whitespace, outside quotes. An apostrophe in plain text opens a
    quote, so a comment after one is kept, which only errs towards a use."""
    quote = ""
    for i, char in enumerate(line):
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _workflow_lines(root: Path) -> list[tuple[str, list[str]]]:
    """Each workflow in ``root/.github/workflows``, by file name, as its lines without comments."""
    return [
        (path.name, [_without_comment(line) for line in path.read_text(encoding="utf-8").splitlines()])
        for path in sorted((root / WORKFLOWS).glob("*.y*ml"))
    ]


def _uses(lines: list[str]) -> set[str]:
    """The secrets lines use, other than GITHUB_TOKEN, in upper case."""
    return {match.upper() for match in _USE_RE.findall("\n".join(lines))} - {_BUILT_IN}


def workflow_secrets(root: Path = Path()) -> dict[str, list[str]]:
    """The secrets the workflows in ``root/.github/workflows`` use, other than
    GITHUB_TOKEN: each name in upper case, with the names of the files that use it.
    Comments don't count."""
    used: dict[str, list[str]] = {}
    for file, lines in _workflow_lines(root):
        for name in sorted(_uses(lines)):
            used.setdefault(name, []).append(file)
    return used


@dataclass(frozen=True)
class Job:
    # The name of its workflow's file.
    file: str
    # Its id, its key under jobs.
    id: str
    # The environment it declares: None if it declares none, UNKNOWN if an expression gives it.
    environment: str | None
    # The secrets it uses, other than GITHUB_TOKEN, in upper case.
    secrets: frozenset[str]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _environment(body: list[str]) -> str | None:
    """The environment a job's body (the lines under its id) declares."""
    indent = min((_indent(line) for line in body if line.strip()), default=0)
    for i, line in enumerate(body):
        match = _ENVIRONMENT_RE.fullmatch(line.strip()) if _indent(line) == indent else None
        if not match:
            continue
        value = match[1].strip()
        if value and not value.startswith("{"):
            name = _unquote(value)
        else:
            if not value:
                # A block mapping, on the lines indented under environment.
                mapping: list[str] = []
                for nested in body[i + 1 :]:
                    if nested.strip() and _indent(nested) <= indent:
                        break
                    mapping.append(nested.strip())
                value = ",".join(mapping)
            found = _NAME_IN_MAPPING_RE.search(value)
            if not found:
                return UNKNOWN
            name = _unquote(found[1])
        return UNKNOWN if "${{" in name else name
    return None


def _jobs(file: str, lines: list[str]) -> list[Job]:
    """The jobs in a workflow's lines (without comments), read by indentation, as
    YAML's block style lays them out. A workflow written another way has none."""
    start = next((i for i, line in enumerate(lines) if _JOBS_RE.fullmatch(line.rstrip())), None)
    if start is None:
        return []
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not _indent(line):
            break
        block.append(line)
    job_indent = next((_indent(line) for line in block if line.strip()), 0)
    # Each job's id, its body, and whether it's written on one line (as a flow mapping).
    bodies: list[tuple[str, list[str], bool]] = []
    for line in block:
        if line.strip() and _indent(line) == job_indent:
            match = _BLOCK_KEY_RE.fullmatch(line.strip())
            if match:
                bodies.append((match[1], [match[2] or ""], bool(match[2] and match[2].strip())))
        elif bodies:
            bodies[-1][1].append(line)
    return [
        Job(file, job_id, UNKNOWN if flow else _environment(body[1:]), frozenset(_uses(body)))
        for job_id, body, flow in bodies
    ]


def workflow_jobs(root: Path = Path()) -> list[Job]:
    """The jobs of every workflow in ``root/.github/workflows``."""
    return [job for file, lines in _workflow_lines(root) for job in _jobs(file, lines)]


def undeclared_environments(listed: Listed, jobs: Iterable[Job]) -> list[str]:
    """A line for each secret a job uses that only environments hold, without
    declaring one of them. A job whose environment an expression gives is skipped."""
    repository = {name.upper() for names in listed.lists.values() for name in names}
    holders: dict[str, list[str]] = {}
    for environment, names in (listed.environments or {}).items():
        for name in names:
            if name.upper() not in repository:
                holders.setdefault(name.upper(), []).append(environment)
    problems = []
    for job in jobs:
        if job.environment == UNKNOWN:
            continue
        for name in sorted(job.secrets & holders.keys()):
            environments = holders[name]
            if job.environment is None or job.environment.lower() not in {env.lower() for env in environments}:
                declared = f"declares environment {job.environment}" if job.environment else "declares no environment"
                problems.append(
                    f"{name}: used by job {job.id} in {job.file}, which {declared}, "
                    f"but only environment {' or '.join(environments)} holds it"
                )
    return problems


def check_workflows(root: Path = Path()) -> int:
    loaded = _load(root)
    listed: dict[str, str] = {}
    for name in loaded.all_names():
        listed.setdefault(name.upper(), name)
    used = workflow_secrets(root)
    unlisted = sorted(set(used) - set(listed))
    unused = sorted(listed[name] for name in set(listed) - set(used))
    undeclared = undeclared_environments(loaded, workflow_jobs(root))
    for name in unlisted:
        print(f"{name}: used by {', '.join(used[name])}, but not listed in pyproject.toml")
    for name in unused:
        print(f"{name}: listed in pyproject.toml, but no workflow uses it")
    for line in undeclared:
        print(line)
    if unlisted:
        print(
            f"{len(unlisted)} secret(s) the workflows use aren't listed in pyproject.toml: add each to {TABLE}, "
            f"under actions, dependabot or environments.<name>, or under {OTHER} if the repository doesn't hold it."
        )
    if unused:
        print(f"{len(unused)} secret(s) pyproject.toml lists aren't used by any workflow: remove them from {TABLE}.")
    if undeclared:
        print(
            f"{len(undeclared)} use(s) of an environment's secret are in a job that doesn't deploy to it, "
            "so the secret is empty there: give the job that environment."
        )
    if unlisted or unused or undeclared:
        return 1
    print(f"The workflows use exactly the {len(listed)} secret(s) pyproject.toml lists.")
    return 0


def _secret_names(url: str, token: str) -> list[str]:
    """The names of every secret the list at url gives."""
    names: list[str] = []
    page = 1
    while True:
        batch = github.request("GET", f"{url}?per_page={PER_PAGE}&page={page}", token)["secrets"]
        names += [secret["name"] for secret in batch]
        if len(batch) < PER_PAGE:
            return names
        page += 1


def repo_secrets(repo: str, token: str, kind: Kind) -> list[str]:
    """The names of every secret of kind the repository has."""
    return _secret_names(f"{github.API_URL}/repos/{repo}/{kind.path}", token)


def environment_secrets(repo: str, token: str, environment: str) -> list[str]:
    """The names of every secret the repository's environment has."""
    quoted = urllib.parse.quote(environment, safe="")
    return _secret_names(f"{github.API_URL}/repos/{repo}/environments/{quoted}/secrets", token)


def repo_environments(repo: str, token: str) -> list[str]:
    """The name of every environment the repository has."""
    return [env["name"] for env in environments_command.repo_environments(repo, token)]


def missing_secrets(names: Iterable[str], existing: Iterable[str]) -> list[str]:
    """The names in names that existing lacks, ignoring case."""
    theirs = {name.upper() for name in existing}
    return [name for name in names if name.upper() not in theirs]


def _toml_list(key: str, names: list[str]) -> list[str]:
    if not names:
        return [f"{key} = []"]
    return [f"{key} = [", *(f"    {config.toml_string(name)}," for name in sorted(names)), "]"]


def dump_toml(existing: dict[str, list[str]], environments: dict[str, list[str]]) -> str:
    """The secret names of each kind as TABLE, with every kind, sorted, and then
    each environment's (``environments = {}`` if there are none)."""
    lines = [TABLE]
    for key, names in existing.items():
        lines += _toml_list(key, names)
    if not environments:
        lines.append(f"{ENVIRONMENTS} = {{}}")
    for name in sorted(environments, key=str.lower):
        lines += _toml_list(f"{ENVIRONMENTS}.{config.toml_key(name)}", environments[name])
    return "".join(f"{line}\n" for line in lines)


def dump(repo: str, token: str) -> int:
    existing = {kind.key: repo_secrets(repo, token, kind) for kind in KINDS}
    environments = {name: environment_secrets(repo, token, name) for name in repo_environments(repo, token)}
    print(dump_toml(existing, {name: names for name, names in environments.items() if names}), end="")
    return 0


def check(
    repo: str,
    token: str,
    wanted: dict[str, list[str]],
    *,
    exit_zero: bool,
    fail_on_unexpected: bool,
    environments: dict[str, list[str]] | None = None,
) -> int:
    missing_count = unexpected_count = missing_environments = 0
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
    if environments is not None:
        existing_environments = {name.lower(): name for name in repo_environments(repo, token)}
        to_check = dict(environments)
        if fail_on_unexpected:
            listed = {name.lower() for name in environments}
            to_check.update({name: [] for lower, name in existing_environments.items() if lower not in listed})
        for name, names in to_check.items():
            actual = existing_environments.get(name.lower())
            if actual is None:
                print(f"Environment {name}: missing" + (f", with its {len(names)} secret(s)" if names else ""))
                missing_environments += 1
                missing_count += len(names)
                continue
            existing = environment_secrets(repo, token, actual)
            missing = missing_secrets(names, existing)
            unexpected = missing_secrets(existing, names) if fail_on_unexpected else []
            for secret in missing:
                print(f"Environment {actual} secret {secret}: missing")
            for secret in unexpected:
                print(f"Environment {actual} secret {secret}: not listed in pyproject.toml")
            missing_count += len(missing)
            unexpected_count += len(unexpected)
    total = sum(len(names) for names in wanted.values()) + sum(len(names) for names in (environments or {}).values())
    if missing_environments:
        print(
            f"{missing_environments} environment(s) are missing from {repo}: "
            "create them with `repoutils environments ensure` or on the repository's settings page."
        )
    if missing_count:
        print(
            f"{missing_count} of {total} secret(s) are missing from {repo}: "
            "set them with `gh secret set` (`--app dependabot` for a Dependabot secret, "
            "`--env NAME` for an environment's)."
        )
    if unexpected_count:
        print(
            f"{unexpected_count} secret(s) in {repo} aren't listed in pyproject.toml: "
            "add them there (`repoutils secrets dump` prints them) or delete them on GitHub."
        )
    if missing_count or unexpected_count or missing_environments:
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
        "it needs the Secrets and Dependabot secrets read permissions, "
        "and Actions and Environments read for environment secrets. "
        "Organization secrets don't count.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,dump,check-workflows}")
    check_parser = subparsers.add_parser(
        "check",
        help="fail if a secret pyproject.toml lists is missing",
        description="Fail if the repository lacks a secret, or an environment, pyproject.toml lists.",
    )
    dump_parser = subparsers.add_parser(
        "dump",
        help="print the repository's secret names as pyproject.toml settings",
        description=f"Print the name of every secret the repository has as {TABLE}, to paste into pyproject.toml.",
    )
    subparsers.add_parser(
        "check-workflows",
        help="fail if the workflows' secrets and pyproject.toml's differ (needs no token)",
        description="Fail if a workflow in .github/workflows uses a secret pyproject.toml doesn't list, "
        "or pyproject.toml lists one no workflow uses, "
        "or a job uses an environment's secret without deploying to that environment. Needs no token.",
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
        if args.mode == "check-workflows":
            return check_workflows()
        # dump doesn't read pyproject.toml, so it works before there are any secrets there.
        listed = Listed({}, None) if args.mode == "dump" else _load(Path())
        wanted = {key: names for key, names in listed.lists.items() if key != OTHER}
        if not wanted and listed.environments is None and args.mode != "dump":
            print(
                f"No secrets are listed: add actions, dependabot or environments to {TABLE} in pyproject.toml.",
                file=sys.stderr,
            )
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "dump":
            return dump(repo, token)
        return check(
            repo,
            token,
            wanted,
            exit_zero=args.exit_zero,
            fail_on_unexpected=args.fail_on_unexpected,
            environments=listed.environments,
        )
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

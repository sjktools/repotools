"""Check that a repository has the deployment environments pyproject.toml
defines, with the settings it gives, or create and update them so it does.

Each environment is a ``[tool.repoutils.environments.<name>]`` table, which
may be empty (the environment need only exist) or give any of: which branches
and tags may deploy to it (``deployment-branches``, with ``allowed-branches``
and ``allowed-tags`` patterns for ``"selected"``), a wait timer, required
reviewers, whether they may approve their own runs, and whether
administrators may bypass these rules. A setting the table leaves out is left
as it is on GitHub. Environment names match regardless of case, as on GitHub.

``ensure`` creates every missing environment and changes every setting that
differs, then fails if GitHub didn't apply a change. It never deletes an
environment, or touches its secrets and variables. ``check`` fails exactly
when ``ensure`` would change something; with ``--fail-on-unexpected`` it also
fails on environments the repository has that pyproject.toml doesn't define.

``dump`` prints every environment the repository has as these tables, to
paste into pyproject.toml (``check`` then passes) and edit. Stdlib-only, like
every repoutils command."""

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

TABLE = "[tool.repoutils.environments]"
# Environments or branch policies per page of a list (GitHub's most).
PER_PAGE = 100
# GitHub's limits on an environment's wait timer (in minutes) and its required reviewers.
MAX_WAIT_TIMER = 43200
MAX_REVIEWERS = 6
DEPLOYMENT_BRANCHES = ("all", "protected", "selected")
# The settings that are part of the environment itself, set with one PUT; the
# allowed branches and tags are its deployment branch policies, set one by one.
ENVIRONMENT_KEYS = ("deployment-branches", "wait-timer", "reviewers", "prevent-self-review", "can-admins-bypass")
POLICY_KEYS = {"allowed-branches": "branch", "allowed-tags": "tag"}
# Each setting as GitHub has it for an environment created without any.
DEFAULTS: dict[str, Any] = {
    "deployment-branches": "all",
    "allowed-branches": [],
    "allowed-tags": [],
    "wait-timer": 0,
    "reviewers": [],
    "prevent-self-review": False,
    "can-admins-bypass": True,
}
# The order dump writes them in.
KEYS = list(DEFAULTS)
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_REVIEWER_RE = re.compile(r"[A-Za-z0-9-]+(/[A-Za-z0-9._-]+)?")


@dataclass
class Environment:
    name: str
    # Each setting pyproject.toml gives, by its key in the table.
    settings: dict[str, Any] = field(default_factory=dict)


def _string_list(value: Any, where: str, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise config.ConfigError(f"{where} {key} must be a list of non-empty strings")
    if len({item.lower() for item in value}) != len(value):
        raise config.ConfigError(f"{where} {key} lists something twice")
    return list(value)


def _setting(where: str, key: str, value: Any) -> Any:
    """value, checked to be one key can have."""
    if key == "deployment-branches":
        if value not in DEPLOYMENT_BRANCHES:
            raise config.ConfigError(
                f"{where} {key} must be one of {', '.join(map(config.toml_string, DEPLOYMENT_BRANCHES))}"
            )
        return value
    if key in POLICY_KEYS:
        return _string_list(value, where, key)
    if key == "wait-timer":
        if type(value) is not int or not 0 <= value <= MAX_WAIT_TIMER:
            raise config.ConfigError(f"{where} {key} must be a whole number of minutes from 0 to {MAX_WAIT_TIMER}")
        return value
    if key == "reviewers":
        reviewers = _string_list(value, where, key)
        if len(reviewers) > MAX_REVIEWERS:
            raise config.ConfigError(f"{where} {key} can list at most {MAX_REVIEWERS} users or teams")
        for reviewer in reviewers:
            if not _REVIEWER_RE.fullmatch(reviewer):
                raise config.ConfigError(f'{where} {key} must list a user as "login" or a team as "org/team-slug"')
        return reviewers
    if key in ("prevent-self-review", "can-admins-bypass"):
        if type(value) is not bool:
            raise config.ConfigError(f"{where} {key} must be true or false")
        return value
    raise config.ConfigError(f"{where} has an unknown setting: {key}")


def load_environments(root: Path = Path()) -> list[Environment]:
    """The environments ``root/pyproject.toml`` defines, raising
    config.ConfigError (a ValueError) if they're malformed."""
    table = config.section("environments", root)
    environments: list[Environment] = []
    seen: set[str] = set()
    for name, entry in table.items():
        where = f"{TABLE[:-1]}.{_toml_key(name)}]"
        if not isinstance(entry, dict):
            raise config.ConfigError(f"{where} must be a table")
        if name.lower() in seen:
            raise config.ConfigError(f"{where} is defined twice (environment names ignore case)")
        seen.add(name.lower())
        settings = {key: _setting(where, key, value) for key, value in entry.items()}
        if any(key in settings for key in POLICY_KEYS):
            # Allowed branches and tags only apply to selected ones, so imply that.
            selected = settings.setdefault("deployment-branches", "selected")
            if selected != "selected":
                raise config.ConfigError(
                    f'{where} allowed-branches and allowed-tags need deployment-branches = "selected"'
                )
        environments.append(Environment(name, settings))
    return environments


def _toml_key(name: str) -> str:
    return name if _BARE_KEY_RE.fullmatch(name) else config.toml_string(name)


def reviewer_name(owner: str, reviewer: dict[str, Any]) -> str:
    """A required reviewer GitHub returns, as pyproject.toml lists it: a user's
    login, or a team as owner/slug (an environment's teams are its owner's)."""
    who = reviewer.get("reviewer") or {}
    if reviewer.get("type") == "Team" and "slug" in who:
        return f"{owner}/{who['slug']}"
    if "login" in who:
        login: str = who["login"]
        return login
    raise RuntimeError(f"GitHub returned a required reviewer repoutils doesn't understand: {reviewer!r}")


def current(owner: str, existing: dict[str, Any] | None, policies: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Every setting as GitHub has it for existing (DEFAULTS if it's missing),
    with policies, its deployment branch policies."""
    if existing is None:
        return dict(DEFAULTS)
    deployment = existing.get("deployment_branch_policy")
    rules = {rule.get("type"): rule for rule in existing.get("protection_rules") or []}
    reviewers = rules.get("required_reviewers", {})
    result = {
        "deployment-branches": (
            "all" if not deployment else "protected" if deployment.get("protected_branches") else "selected"
        ),
        "wait-timer": rules.get("wait_timer", {}).get("wait_timer", 0),
        "reviewers": [reviewer_name(owner, reviewer) for reviewer in reviewers.get("reviewers") or []],
        "prevent-self-review": bool(reviewers.get("prevent_self_review", False)),
        "can-admins-bypass": bool(existing.get("can_admins_bypass", True)),
    }
    for key, kind in POLICY_KEYS.items():
        # A policy made before tag policies existed has no type.
        result[key] = [policy["name"] for policy in policies if policy.get("type", "branch") == kind]
    return result


def _same(key: str, theirs: Any, ours: Any) -> bool:
    if isinstance(ours, list):
        # Lists are sets, and reviewers' names ignore case.
        fold = str.lower if key == "reviewers" else str
        return sorted(map(fold, theirs)) == sorted(map(fold, ours))
    return bool(theirs == ours)


@dataclass
class Plan:
    """What ensure does for one environment."""

    environment: Environment
    # The environment's current name on GitHub, or None if it's missing.
    existing_name: str | None
    # Every setting as GitHub has it.
    theirs: dict[str, Any]
    changes: list[str] = field(default_factory=list)
    # The settings that differ, by key.
    changed: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    @property
    def needs_ensure(self) -> bool:
        return self.existing_name is None or bool(self.changes)


def plan(environment: Environment, owner: str, existing: dict[str, Any] | None, policies: list[dict[str, Any]]) -> Plan:
    """What ensure does to make existing (GitHub's environment, None if it's missing) match environment."""
    theirs = current(owner, existing, policies)
    result = Plan(environment, None if existing is None else existing["name"], theirs)
    if result.existing_name is not None and result.existing_name != environment.name:
        result.notes.append(f"name is {result.existing_name!r} on GitHub (environments can't be renamed)")
    for key in KEYS:
        if key in environment.settings and not _same(key, theirs[key], environment.settings[key]):
            result.changed.add(key)
            result.changes.append(f"{key} is {theirs[key]!r} on GitHub, not {environment.settings[key]!r}")
    return result


def _environment_url(repo: str, name: str) -> str:
    return f"{github.API_URL}/repos/{repo}/environments/{urllib.parse.quote(name, safe='')}"


def _pages(url: str, token: str, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = github.request("GET", f"{url}?per_page={PER_PAGE}&page={page}", token)[key]
        items += batch
        if len(batch) < PER_PAGE:
            return items
        page += 1


def repo_environments(repo: str, token: str) -> list[dict[str, Any]]:
    """Every environment the repository has."""
    return _pages(f"{github.API_URL}/repos/{repo}/environments", token, "environments")


def branch_policies(repo: str, token: str, existing: dict[str, Any] | None) -> list[dict[str, Any]]:
    """existing's deployment branch policies, which only selected branches and tags have."""
    if existing is None:
        return []
    deployment = existing.get("deployment_branch_policy")
    if not deployment or not deployment.get("custom_branch_policies"):
        return []
    return _pages(f"{_environment_url(repo, existing['name'])}/deployment-branch-policies", token, "branch_policies")


def plans(repo: str, token: str, environments: Iterable[Environment]) -> tuple[list[Plan], list[dict[str, Any]]]:
    """A plan for each environment, and every environment the repository has."""
    owner = repo.split("/", 1)[0]
    existing = repo_environments(repo, token)
    by_name = {env["name"].lower(): env for env in existing}
    results = []
    for environment in environments:
        env = by_name.get(environment.name.lower())
        policies = branch_policies(repo, token, env) if POLICY_KEYS.keys() & environment.settings.keys() else []
        results.append(plan(environment, owner, env, policies))
    return results, existing


def _reviewer_id(token: str, reviewer: str) -> dict[str, Any]:
    """reviewer as the PUT request gives it: its type and ID."""
    org, _, slug = reviewer.partition("/")
    if slug:
        team = github.request("GET", f"{github.API_URL}/orgs/{org}/teams/{slug}", token)
        return {"type": "Team", "id": team["id"]}
    user = github.request("GET", f"{github.API_URL}/users/{reviewer}", token)
    return {"type": "User", "id": user["id"]}


def _deployment_branch_policy(value: str) -> dict[str, bool] | None:
    if value == "all":
        return None
    return {"protected_branches": value == "protected", "custom_branch_policies": value == "selected"}


def apply(repo: str, token: str, p: Plan, existing: dict[str, Any] | None) -> None:
    """Creates or updates p's environment, and its deployment branch policies, as p says."""
    name = p.existing_name or p.environment.name
    ours = p.environment.settings
    if p.existing_name is None or p.changed & set(ENVIRONMENT_KEYS):
        values = {key: ours.get(key, p.theirs[key]) for key in ENVIRONMENT_KEYS}
        if "reviewers" in p.changed:
            reviewers = [_reviewer_id(token, reviewer) for reviewer in values["reviewers"]]
        else:
            rules = (existing or {}).get("protection_rules") or []
            reviewers = [
                {"type": reviewer["type"], "id": reviewer["reviewer"]["id"]}
                for rule in rules
                if rule.get("type") == "required_reviewers"
                for reviewer in rule.get("reviewers") or []
            ]
        # PUT sets every setting, so the ones pyproject.toml leaves out are sent as they are.
        body = {
            "wait_timer": values["wait-timer"],
            "prevent_self_review": values["prevent-self-review"],
            "reviewers": reviewers or None,
            "deployment_branch_policy": _deployment_branch_policy(values["deployment-branches"]),
            "can_admins_bypass": values["can-admins-bypass"],
        }
        existing = github.request("PUT", _environment_url(repo, name), token, json_body=body)
    if not p.changed & POLICY_KEYS.keys():
        return
    # Read them again, as changing deployment-branches may have changed them.
    policies = branch_policies(repo, token, existing)
    url = f"{_environment_url(repo, name)}/deployment-branch-policies"
    for key, kind in POLICY_KEYS.items():
        if key not in p.changed:
            continue
        wanted = set(ours[key])
        have = {policy["name"] for policy in policies if policy.get("type", "branch") == kind}
        for pattern in sorted(wanted - have):
            github.request("POST", url, token, json_body={"name": pattern, "type": kind})
        for policy in policies:
            if policy.get("type", "branch") == kind and policy["name"] not in wanted:
                github.request("DELETE", f"{url}/{policy['id']}", token)


def _report(p: Plan, *, done: bool) -> list[str]:
    name = p.environment.name
    lines = []
    if p.needs_ensure:
        if p.existing_name is None:
            status = "created" if done else "missing"
        else:
            status = "updated" if done else "needs updating"
        lines.append(f"{name}: {status}: {'; '.join(p.changes)}" if p.changes else f"{name}: {status}")
    lines += [f"{name}: {note}" for note in p.notes]
    return lines


def dump_toml(owner: str, existing: Iterable[tuple[dict[str, Any], list[dict[str, Any]]]]) -> str:
    """GitHub's environments, each with its deployment branch policies, as TABLE's sub-tables."""
    entries = []
    for env, policies in existing:
        values = current(owner, env, policies)
        lines = [f"{TABLE[:-1]}.{_toml_key(env['name'])}]"]
        for key in KEYS:
            if key in POLICY_KEYS and values["deployment-branches"] != "selected":
                continue
            value = values[key]
            if isinstance(value, list):
                text = "[" + ", ".join(config.toml_string(item) for item in sorted(value)) + "]"
            elif isinstance(value, int) and not isinstance(value, bool):
                text = str(value)
            else:
                text = config.toml_value(value)
            lines.append(f"{key} = {text}")
        entries.append("".join(f"{line}\n" for line in lines))
    return "\n".join(entries)


def dump(repo: str, token: str) -> int:
    existing = repo_environments(repo, token)
    if not existing:
        print(f"{repo} has no environments.", file=sys.stderr)
    owner = repo.split("/", 1)[0]
    print(dump_toml(owner, [(env, branch_policies(repo, token, env)) for env in existing]), end="")
    return 0


def unexpected_environments(environments: Iterable[Environment], existing: Iterable[dict[str, Any]]) -> list[str]:
    """The names of the environments in existing that environments doesn't define, ignoring case."""
    ours = {environment.name.lower() for environment in environments}
    return [env["name"] for env in existing if env["name"].lower() not in ours]


def check(repo: str, token: str, environments: list[Environment], *, exit_zero: bool, fail_on_unexpected: bool) -> int:
    results, existing = plans(repo, token, environments)
    for p in results:
        for line in _report(p, done=False):
            print(line)
    unexpected = unexpected_environments(environments, existing) if fail_on_unexpected else []
    for name in unexpected:
        print(f"{name}: not defined in pyproject.toml")
    failing = sum(p.needs_ensure for p in results)
    if failing:
        print(f"{failing} of {len(environments)} environment(s) in {repo} need `repoutils environments ensure`.")
    if unexpected:
        print(
            f"{len(unexpected)} environment(s) in {repo} aren't defined in pyproject.toml: "
            "add them there (`repoutils environments dump` prints them) or delete them on GitHub."
        )
    if failing or unexpected:
        return 0 if exit_zero else 1
    print(f"All {len(environments)} environment(s) in {repo} match pyproject.toml.")
    return 0


def ensure(repo: str, token: str, environments: list[Environment]) -> int:
    results, existing = plans(repo, token, environments)
    by_name = {env["name"]: env for env in existing}
    for p in results:
        apply(repo, token, p, by_name.get(p.existing_name or ""))
        for line in _report(p, done=True):
            print(line)
    # GitHub may ignore a setting (one the plan doesn't allow, say) rather than refuse it, so check each.
    unapplied, _ = plans(repo, token, environments)
    failed = [p for p in unapplied if p.needs_ensure]
    for p in failed:
        for change in p.changes or ["missing"]:
            print(f"GitHub didn't apply the change: {p.environment.name}: {change}", file=sys.stderr)
    if failed:
        return 1
    changed = sum(p.needs_ensure for p in results)
    print(f"Created or updated {changed} of {len(environments)} environment(s) in {repo}.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils environments",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`; ensure needs a token that can administer "
        "the repository, which Actions' GITHUB_TOKEN can't be given.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,ensure,dump}")
    check_parser = subparsers.add_parser(
        "check", help="fail if ensure would change anything", description="Fail if ensure would change anything."
    )
    ensure_parser = subparsers.add_parser(
        "ensure",
        help="create and update environments to match",
        description="Create and update environments to match.",
    )
    dump_parser = subparsers.add_parser(
        "dump",
        help="print the repository's environments as pyproject.toml tables",
        description=f"Print every environment the repository has as {TABLE} sub-tables, to paste into pyproject.toml.",
    )
    for sub in (check_parser, ensure_parser, dump_parser):
        sub.add_argument(
            "--repo",
            default=os.environ.get("GITHUB_REPOSITORY"),
            help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
        )
    check_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="exit 0 even if environments need ensure (still exit 1 on an error, unlike `|| true`)",
    )
    check_parser.add_argument(
        "--fail-on-unexpected",
        action="store_true",
        help="also fail on environments the repository has that pyproject.toml doesn't define "
        "(ensure leaves them alone)",
    )
    args = parser.parse_args(argv)

    try:
        # dump doesn't read pyproject.toml, so it works before there are any environments there.
        environments = [] if args.mode == "dump" else load_environments()
        if not environments and args.mode != "dump":
            print(f"No environments are defined: add {TABLE[:-1]}.<name>] to pyproject.toml.", file=sys.stderr)
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "dump":
            return dump(repo, token)
        if args.mode == "check":
            return check(
                repo, token, environments, exit_zero=args.exit_zero, fail_on_unexpected=args.fail_on_unexpected
            )
        return ensure(repo, token, environments)
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

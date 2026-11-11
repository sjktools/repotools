"""Check that a repository has the branch rulesets pyproject.toml defines, or
create and update them so it does.

Each ruleset is a ``[tool.repoutils.rulesets.<name>]`` table that gives only
what differs from a new ruleset's defaults: anything it leaves out is off (a
rule), empty (``include``, ``exclude`` and ``bypass``) or the default (a
rule's parameter, and ``enforcement``, which is ``"active"``). So the table is
the whole ruleset, and ``ensure`` turns off what it leaves out. Rules and
their parameters have the names GitHub's REST API (and a ruleset's JSON
export) gives them, in kebab case. GitHub Apps and teams are named by slug,
which ``check`` and ``ensure`` look up the IDs of.

Only the repository's own branch rulesets are managed: not tag or push
rulesets, ones it inherits from its organization, or legacy branch
protection rules. A rule parameter repoutils doesn't know of is ignored.

``ensure`` creates every missing ruleset and replaces every one that differs,
then fails if GitHub didn't apply a change. It never deletes a ruleset.
``check`` fails exactly when ``ensure`` would change something; with
``--fail-on-unexpected`` it also fails on branch rulesets the repository has
that pyproject.toml doesn't define.

``dump`` prints every branch ruleset the repository has (or that ruleset JSON
exports from GitHub give) as these tables, to paste into pyproject.toml
(``check`` then passes) and edit. GitHub has no API that gives a GitHub App's
slug from its ID, so ``dump`` finds slugs from the apps that ran checks on the
head of the default branch and the branches the rulesets name; one it can't
find is given as its ID, with a warning. Stdlib-only, like every repoutils
command."""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoutils import github
from repoutils.ruleset_format import (
    TABLE,
    Names,
    diff,
    dump_toml,
    from_github,
    load_rulesets,
    short_ref,
    to_github,
)

# Rulesets, check runs or teams per page of a list (GitHub's most).
PER_PAGE = 100
# Apps dump looks up when it can't find an app's slug from the checks that ran.
KNOWN_APPS = ("github-actions", "dependabot", "codecov")


# GitHub's API.


def _get_or_none(url: str, token: str) -> Any:
    """The response to GET url, or None if GitHub answers 404."""
    try:
        return github.request("GET", url, token)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        e.close()
        return None


def repo_rulesets(repo: str, token: str) -> list[dict[str, Any]]:
    """A summary of each of the repository's own branch rulesets."""
    rulesets: list[dict[str, Any]] = []
    page = 1
    while True:
        url = f"{github.API_URL}/repos/{repo}/rulesets?includes_parents=false&per_page={PER_PAGE}&page={page}"
        batch = github.request("GET", url, token)
        rulesets += [
            r for r in batch if r.get("target") == "branch" and r.get("source_type", "Repository") == "Repository"
        ]
        if len(batch) < PER_PAGE:
            return rulesets
        page += 1


def get_ruleset(repo: str, token: str, ruleset_id: int) -> dict[str, Any]:
    ruleset: dict[str, Any] = github.request("GET", f"{github.API_URL}/repos/{repo}/rulesets/{ruleset_id}", token)
    return ruleset


def resolve(repo: str, token: str, rulesets: Iterable[dict[str, Any]]) -> Names:
    """The IDs of the apps and teams the rulesets name."""
    names = Names()
    owner = repo.split("/", 1)[0]
    for ruleset in rulesets:
        checks = ruleset.get("rules", {}).get("required-status-checks", {}).get("required-status-checks", [])
        apps = [actor["app"] for actor in ruleset.get("bypass", []) if "app" in actor]
        for slug in apps + [check["app"] for check in checks if "app" in check]:
            if slug not in names.apps:
                app = _get_or_none(f"{github.API_URL}/apps/{urllib.parse.quote(slug, safe='')}", token)
                if app is None:
                    raise RuntimeError(f"There's no GitHub App with the slug {slug!r}")
                names.apps[slug] = app["id"]
        for slug in (actor["team"] for actor in ruleset.get("bypass", []) if "team" in actor):
            if slug not in names.teams:
                url = f"{github.API_URL}/orgs/{owner}/teams/{urllib.parse.quote(slug, safe='')}"
                team = _get_or_none(url, token)
                if team is None:
                    raise RuntimeError(f"{owner} has no team with the slug {slug!r}")
                names.teams[slug] = team["id"]
    return names


def _ids(rulesets: Iterable[dict[str, Any]], kind: str) -> set[Any]:
    """The IDs of the apps (kind "Integration") or teams ("Team") the rulesets name."""
    ids = {a.get("actor_id") for r in rulesets for a in r.get("bypass_actors") or [] if a.get("actor_type") == kind}
    if kind == "Integration":
        for rule in (rule for r in rulesets for rule in r.get("rules") or []):
            if rule["type"] == "required_status_checks":
                checks = (rule.get("parameters") or {}).get("required_status_checks") or []
                ids |= {c["integration_id"] for c in checks if c.get("integration_id") is not None}
    return ids


def discover(repo: str, token: str, rulesets: list[dict[str, Any]]) -> Names:
    """The slugs of as many of the apps and teams the rulesets name as dump can find."""
    names = Names()
    app_ids, team_ids = _ids(rulesets, "Integration"), _ids(rulesets, "Team")
    if app_ids:
        branches = [github.request("GET", f"{github.API_URL}/repos/{repo}", token)["default_branch"]]
        for r in rulesets:
            for pattern in ((r.get("conditions") or {}).get("ref_name") or {}).get("include") or []:
                branch = short_ref(pattern)
                if not branch.startswith(("~", "refs/")) and not set("*?[") & set(branch) and branch not in branches:
                    branches.append(branch)
        for branch in branches:
            ref = urllib.parse.quote(branch, safe="/")
            for kind in ("check-runs", "check-suites"):
                found = _get_or_none(f"{github.API_URL}/repos/{repo}/commits/{ref}/{kind}?per_page={PER_PAGE}", token)
                for item in (found or {}).get(kind.replace("-", "_"), []):
                    if item.get("app"):
                        names.apps[item["app"]["slug"]] = item["app"]["id"]
        for slug in KNOWN_APPS:
            if app_ids - set(names.apps.values()) and slug not in names.apps:
                app = _get_or_none(f"{github.API_URL}/apps/{slug}", token)
                if app is not None:
                    names.apps[slug] = app["id"]
    if team_ids:
        teams = _get_or_none(f"{github.API_URL}/orgs/{repo.split('/', 1)[0]}/teams?per_page={PER_PAGE}", token)
        names.teams = {team["slug"]: team["id"] for team in teams or []}
    return names


def dump(repo: str, token: str, files: list[str]) -> int:
    if files:
        rulesets = []
        for path in files:
            loaded = json.loads(Path(path).read_text())
            rulesets += loaded if isinstance(loaded, list) else [loaded]
        for r in [r for r in rulesets if r.get("target", "branch") != "branch"]:
            print(f"Left out {r.get('name')!r}: it's a {r.get('target')} ruleset, not a branch one", file=sys.stderr)
        rulesets = [r for r in rulesets if r.get("target", "branch") == "branch"]
    else:
        rulesets = [get_ruleset(repo, token, r["id"]) for r in repo_rulesets(repo, token)]
        if not rulesets:
            print(f"{repo} has no branch rulesets.", file=sys.stderr)
    text, warnings = dump_toml(rulesets, discover(repo, token, rulesets))
    print(text, end="")
    for warning in warnings:
        print(warning, file=sys.stderr)
    return 0


@dataclass
class Plan:
    name: str
    ruleset: dict[str, Any]
    # The ruleset's ID on GitHub, or None if it's missing.
    existing_id: int | None
    changes: list[str] = field(default_factory=list)

    @property
    def needs_ensure(self) -> bool:
        return self.existing_id is None or bool(self.changes)


def plans(repo: str, token: str, rulesets: dict[str, dict[str, Any]]) -> tuple[list[Plan], list[str], Names]:
    """What ensure does for each ruleset, the names of every branch ruleset the
    repository has, and the IDs of the apps and teams the rulesets name."""
    existing = {r["name"]: r["id"] for r in repo_rulesets(repo, token)}
    names = resolve(repo, token, rulesets.values())
    result = []
    for name, ruleset in rulesets.items():
        ruleset_id = existing.get(name)
        changes = [] if ruleset_id is None else diff(from_github(get_ruleset(repo, token, ruleset_id), names), ruleset)
        result.append(Plan(name, ruleset, ruleset_id, changes))
    return result, list(existing), names


def _report(p: Plan, *, done: bool) -> list[str]:
    if p.existing_id is None:
        return [f"{p.name}: {'created' if done else 'missing'}"]
    if p.changes:
        return [f"{p.name}: {'updated' if done else 'needs updating'}: {'; '.join(p.changes)}"]
    return []


def check(
    repo: str, token: str, rulesets: dict[str, dict[str, Any]], *, exit_zero: bool, fail_on_unexpected: bool
) -> int:
    results, existing, _ = plans(repo, token, rulesets)
    for p in results:
        for line in _report(p, done=False):
            print(line)
    unexpected = [name for name in existing if name not in rulesets] if fail_on_unexpected else []
    for name in unexpected:
        print(f"{name}: not defined in pyproject.toml")
    failing = sum(p.needs_ensure for p in results)
    if failing:
        print(f"{failing} of {len(rulesets)} ruleset(s) in {repo} need `repoutils rulesets ensure`.")
    if unexpected:
        print(
            f"{len(unexpected)} branch ruleset(s) in {repo} aren't defined in pyproject.toml: "
            "add them there (`repoutils rulesets dump` prints them) or delete them on GitHub."
        )
    if failing or unexpected:
        return 0 if exit_zero else 1
    print(f"All {len(rulesets)} ruleset(s) in {repo} match pyproject.toml.")
    return 0


def ensure(repo: str, token: str, rulesets: dict[str, dict[str, Any]]) -> int:
    results, _, names = plans(repo, token, rulesets)
    unapplied = []
    for p in results:
        if not p.needs_ensure:
            continue
        body = to_github(p.name, p.ruleset, names)
        url = f"{github.API_URL}/repos/{repo}/rulesets"
        if p.existing_id is None:
            updated = github.request("POST", url, token, json_body=body)
        else:
            updated = github.request("PUT", f"{url}/{p.existing_id}", token, json_body=body)
        for line in _report(p, done=True):
            print(line)
        # GitHub may ignore a setting rather than refuse it, so check each.
        unapplied += [f"{p.name}: {change}" for change in diff(from_github(updated, names), p.ruleset)]
    for change in unapplied:
        print(f"GitHub didn't apply the change: {change}", file=sys.stderr)
    if unapplied:
        return 1
    changed = sum(p.needs_ensure for p in results)
    print(f"Created or updated {changed} of {len(rulesets)} ruleset(s) in {repo}.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils rulesets",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`, which must be able to administer the "
        "repository (Actions' GITHUB_TOKEN can't be given that): GitHub shows a ruleset's bypass list only "
        "to a token that can edit it.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,ensure,dump}")
    check_parser = subparsers.add_parser(
        "check", help="fail if ensure would change anything", description="Fail if ensure would change anything."
    )
    ensure_parser = subparsers.add_parser(
        "ensure", help="create and update rulesets to match", description="Create and update rulesets to match."
    )
    dump_parser = subparsers.add_parser(
        "dump",
        help="print the repository's branch rulesets as pyproject.toml tables",
        description=f"Print every branch ruleset the repository has as {TABLE} sub-tables, to paste into "
        "pyproject.toml.",
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
        help="exit 0 even if rulesets need ensure (still exit 1 on an error, unlike `|| true`)",
    )
    check_parser.add_argument(
        "--fail-on-unexpected",
        action="store_true",
        help="also fail on branch rulesets the repository has that pyproject.toml doesn't define "
        "(ensure leaves them alone)",
    )
    dump_parser.add_argument(
        "--from-file",
        action="append",
        default=[],
        metavar="FILE",
        help="print the rulesets in a JSON file exported from GitHub's ruleset page instead (repeatable); "
        "the repository is still read to find app slugs",
    )
    args = parser.parse_args(argv)

    try:
        # dump doesn't read pyproject.toml, so it works before there are any rulesets there.
        rulesets = {} if args.mode == "dump" else load_rulesets()
        if not rulesets and args.mode != "dump":
            print(f"No rulesets are defined: add {TABLE[:-1]}.<name>] to pyproject.toml.", file=sys.stderr)
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "dump":
            return dump(repo, token, args.from_file)
        if args.mode == "check":
            return check(repo, token, rulesets, exit_zero=args.exit_zero, fail_on_unexpected=args.fail_on_unexpected)
        return ensure(repo, token, rulesets)
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

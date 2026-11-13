"""Check that a repository's GitHub Actions settings match pyproject.toml, or
change them so they do.

``[tool.repoutils.actions-settings]`` covers the repository's Settings,
Actions, General page: which actions and reusable workflows may run (with a
``selected-actions`` sub-table for ``"selected"``), whether actions must be
pinned to a commit SHA, the default ``GITHUB_TOKEN`` permissions, whether
Actions may create and approve pull requests, which fork pull request
contributors need approval, how long runs, artifacts, logs and caches are kept,
the cache size limit, and the OIDC subject claim (an ``oidc`` sub-table). A
setting the table leaves out is left as it is on GitHub.

``ensure`` changes every setting that differs, one request for each of
GitHub's endpoints that needs one, and fails if GitHub didn't apply a change.
``check`` fails exactly when ``ensure`` would change something.

``dump`` prints every setting the repository has as the table, to paste into
pyproject.toml (``check`` then passes) and edit. Stdlib-only, like every
repoutils command."""

import argparse
import os
import subprocess
import sys
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override

from repoutils import config, github

TABLE = "[tool.repoutils.actions-settings]"
SUB_TABLES = ("selected-actions", "oidc")


@dataclass(frozen=True)
class Endpoint:
    # Its path under the repository's API URL.
    path: str
    # The fields its PUT takes, which ensure sends back as GitHub has them
    # unless it changes them, as some PUTs need fields the table may leave out.
    fields: tuple[str, ...]


PERMISSIONS = Endpoint("actions/permissions", ("enabled", "allowed_actions", "sha_pinning_required"))
SELECTED = Endpoint(
    "actions/permissions/selected-actions", ("github_owned_allowed", "verified_allowed", "patterns_allowed")
)
WORKFLOW = Endpoint(
    "actions/permissions/workflow", ("default_workflow_permissions", "can_approve_pull_request_reviews")
)
FORK = Endpoint("actions/permissions/fork-pr-contributor-approval", ("approval_policy",))
RETENTION = Endpoint("actions/permissions/artifact-and-log-retention", ("days",))
CACHE_RETENTION = Endpoint("actions/cache/retention-limit", ("max_cache_retention_days",))
CACHE_SIZE = Endpoint("actions/cache/storage-limit", ("max_cache_size_gb",))
OIDC = Endpoint("actions/oidc/customization/sub", ("use_default", "include_claim_keys", "use_immutable_subject"))
# The order ensure changes them in: selected-actions can only be set once
# allowed-actions is "selected".
ENDPOINTS = [PERMISSIONS, SELECTED, WORKFLOW, FORK, RETENTION, CACHE_RETENTION, CACHE_SIZE, OIDC]


@dataclass(frozen=True)
class Setting:
    # Its key in TABLE, with a dot for a sub-table's key ("oidc.immutable-subject").
    key: str
    endpoint: Endpoint
    # The endpoint's field.
    api_field: str
    # The value's type: bool, int (a positive number), list (of strings) or
    # str (an enum, see choices).
    kind: type
    # For an enum, each config value and the API value it stands for.
    choices: Mapping[str, str] | None = field(default=None, compare=False)
    # For a list, whether its order doesn't matter.
    unordered: bool = False

    def read(self, response: dict[str, Any]) -> Any:
        """The value in GitHub's response, as pyproject.toml would write it,
        raising KeyError if GitHub didn't return it."""
        raw = response[self.api_field]
        if self.choices is not None and isinstance(raw, str):
            return {api: ours for ours, api in self.choices.items()}.get(raw, raw)
        return raw

    def write(self, value: Any) -> dict[str, Any]:
        """The fields that set value."""
        return {self.api_field: self.choices[value] if self.choices is not None else value}

    def same(self, theirs: Any, ours: Any) -> bool:
        if self.unordered and isinstance(theirs, list):
            return sorted(map(str, theirs)) == sorted(ours)
        return bool(theirs == ours)

    def problem(self, value: Any) -> str | None:
        """What a value TABLE gives must be that value isn't, or None if it's fine."""
        if self.choices is not None:
            if isinstance(value, str) and value in self.choices:
                return None
            return "one of " + ", ".join(f'"{choice}"' for choice in self.choices)
        if self.kind is bool:
            return None if type(value) is bool else "true or false"
        if self.kind is int:
            return None if type(value) is int and value > 0 else "a positive integer"
        if (
            isinstance(value, list)
            and all(isinstance(item, str) and item for item in value)
            and len(set(value)) == len(value)
        ):
            return None
        return "a list of different non-empty strings"


class AllowedActions(Setting):
    """Which actions may run, where "disabled" stands for Actions being off."""

    @override
    def read(self, response: dict[str, Any]) -> Any:
        return "disabled" if response["enabled"] is False else super().read(response)

    @override
    def write(self, value: Any) -> dict[str, Any]:
        return {"enabled": False} if value == "disabled" else {"enabled": True, **super().write(value)}


SETTINGS = [
    AllowedActions(
        "allowed-actions",
        PERMISSIONS,
        "allowed_actions",
        str,
        {"all": "all", "owner": "local_only", "selected": "selected", "disabled": "disabled"},
    ),
    Setting("require-sha-pinning", PERMISSIONS, "sha_pinning_required", bool),
    Setting(
        "default-token-permissions", WORKFLOW, "default_workflow_permissions", str, {"read": "read", "write": "write"}
    ),
    Setting("can-approve-pull-requests", WORKFLOW, "can_approve_pull_request_reviews", bool),
    Setting(
        "fork-pr-approval",
        FORK,
        "approval_policy",
        str,
        {
            "first-time-contributors-new-to-github": "first_time_contributors_new_to_github",
            "first-time-contributors": "first_time_contributors",
            "all-external-contributors": "all_external_contributors",
        },
    ),
    Setting("retention-days", RETENTION, "days", int),
    Setting("cache-retention-days", CACHE_RETENTION, "max_cache_retention_days", int),
    Setting("cache-size-gb", CACHE_SIZE, "max_cache_size_gb", int),
    Setting("selected-actions.github-owned", SELECTED, "github_owned_allowed", bool),
    Setting("selected-actions.verified-creators", SELECTED, "verified_allowed", bool),
    Setting("selected-actions.patterns", SELECTED, "patterns_allowed", list, unordered=True),
    Setting("oidc.use-default-subject", OIDC, "use_default", bool),
    Setting("oidc.include-claim-keys", OIDC, "include_claim_keys", list),
    Setting("oidc.immutable-subject", OIDC, "use_immutable_subject", bool),
]
BY_KEY = {setting.key: setting for setting in SETTINGS}


def _consistency_problem(values: Mapping[str, Any]) -> str | None:
    """Why settings, by key, can't all hold at once, or None if they can."""
    if any(key.startswith("selected-actions.") for key in values) and values.get("allowed-actions") != "selected":
        return 'selected-actions needs allowed-actions = "selected"'
    if "oidc.include-claim-keys" in values and values.get("oidc.use-default-subject") is not False:
        return "oidc.include-claim-keys needs oidc.use-default-subject = false, as GitHub ignores it otherwise"
    return None


def load_settings(root: Path = Path()) -> dict[Setting, Any]:
    """Each setting ``root/pyproject.toml`` gives, and its value, raising
    config.ConfigError (a ValueError) if the table is malformed."""
    table = config.section("actions-settings", root)
    flat: dict[str, Any] = {}
    for key, value in table.items():
        if key in SUB_TABLES and isinstance(value, dict):
            flat.update({f"{key}.{sub}": subvalue for sub, subvalue in value.items()})
        else:
            flat[key] = value
    settings: dict[Setting, Any] = {}
    for key, value in flat.items():
        setting = BY_KEY.get(key)
        if setting is None:
            raise config.ConfigError(f"{TABLE} has an unknown setting: {key}")
        problem = setting.problem(value)
        if problem is not None:
            raise config.ConfigError(f"{TABLE} {key} must be {problem}")
        settings[setting] = value
    problem = _consistency_problem(flat)
    if problem is not None:
        raise config.ConfigError(f"{TABLE} {problem}")
    return settings


def url(repo: str, endpoint: Endpoint) -> str:
    return f"{github.API_URL}/repos/{repo}/{endpoint.path}"


def _selected_in_effect(permissions: dict[str, Any]) -> bool:
    return permissions.get("enabled") is True and permissions.get("allowed_actions") == "selected"


def fetch(repo: str, token: str, endpoints: set[Endpoint]) -> dict[Endpoint, dict[str, Any] | None]:
    """GitHub's response for each endpoint, in ENDPOINTS' order. SELECTED's is
    None if allowed-actions isn't "selected", as GitHub then refuses to give it."""
    responses: dict[Endpoint, dict[str, Any] | None] = {}
    for endpoint in ENDPOINTS:
        if endpoint not in endpoints:
            continue
        permissions = responses.get(PERMISSIONS)
        if endpoint is SELECTED and permissions is not None and not _selected_in_effect(permissions):
            responses[endpoint] = None
        else:
            responses[endpoint] = github.request("GET", url(repo, endpoint), token)
    return responses


def diff(settings: dict[Setting, Any], response: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """The body of the PUT that makes one endpoint's response match settings
    (all of that endpoint's), or {} if it matches, and a note on each difference."""
    endpoint = next(iter(settings)).endpoint
    writes: dict[str, Any] = {}
    changes: list[str] = []
    if response is None:
        for setting, ours in settings.items():
            writes.update(setting.write(ours))
            changes.append(f"{setting.key} isn't in effect on GitHub, as allowed-actions isn't \"selected\"")
        return writes, changes
    missing: list[str] = []
    for setting, ours in settings.items():
        try:
            theirs = setting.read(response)
        except KeyError as e:
            if not response:
                missing.append(str(e.args[0]))
            else:
                # GitHub leaves out some fields it has no value for, such as
                # the claim keys with the default template.
                writes.update(setting.write(ours))
                changes.append(f"{setting.key} is unset on GitHub, not {ours!r}")
            continue
        if not setting.same(theirs, ours):
            writes.update(setting.write(ours))
            changes.append(f"{setting.key} is {theirs!r} on GitHub, not {ours!r}")
    if missing:
        raise RuntimeError(
            f"GitHub didn't return {', '.join(missing)} from {endpoint.path}: "
            "the token may not have access to the repository's Actions settings"
        )
    if not writes:
        return {}, []
    body = {name: response[name] for name in endpoint.fields if name in response} | writes
    if endpoint is OIDC and body.get("use_default") is True:
        # GitHub ignores the keys with the default template, so they needn't be valid.
        body.pop("include_claim_keys", None)
    return body, changes


def by_endpoint(settings: dict[Setting, Any]) -> dict[Endpoint, dict[Setting, Any]]:
    """settings grouped by endpoint, in ENDPOINTS' order."""
    grouped: dict[Endpoint, dict[Setting, Any]] = {endpoint: {} for endpoint in ENDPOINTS}
    for setting, value in settings.items():
        grouped[setting.endpoint][setting] = value
    return {endpoint: group for endpoint, group in grouped.items() if group}


def changes(repo: str, token: str, settings: dict[Setting, Any]) -> list[str]:
    """A note on each setting that differs on GitHub."""
    grouped = by_endpoint(settings)
    responses = fetch(repo, token, set(grouped))
    return [change for endpoint in responses for change in diff(grouped[endpoint], responses[endpoint])[1]]


def check(repo: str, token: str, settings: dict[Setting, Any], *, exit_zero: bool) -> int:
    differences = changes(repo, token, settings)
    for change in differences:
        print(change)
    if differences:
        print(f"{len(differences)} of {len(settings)} setting(s) in {repo} need `repoutils actions-settings ensure`.")
        return 0 if exit_zero else 1
    print(f"All {len(settings)} setting(s) in {repo} match pyproject.toml.")
    return 0


def ensure(repo: str, token: str, settings: dict[Setting, Any]) -> int:
    changed = 0
    for endpoint, endpoint_settings in by_endpoint(settings).items():
        # pyproject.toml gives selected-actions only with allowed-actions =
        # "selected", which ensure has set by now, so GitHub gives it.
        body, endpoint_changes = diff(endpoint_settings, github.request("GET", url(repo, endpoint), token))
        if body:
            github.request("PUT", url(repo, endpoint), token, json_body=body)
            for change in endpoint_changes:
                print(f"updated: {change}")
            changed += len(endpoint_changes)
    if changed:
        try:
            unapplied = changes(repo, token, settings)
        except RuntimeError as e:
            print(f"Updated {repo}, but couldn't check GitHub applied every change: {e}", file=sys.stderr)
            return 1
        if unapplied:
            for change in unapplied:
                print(f"GitHub didn't apply the change: {change}", file=sys.stderr)
            return 1
    print(f"Updated {changed} of {len(settings)} setting(s) in {repo}.")
    return 0


def dump_toml(responses: Mapping[Endpoint, dict[str, Any] | None]) -> tuple[str, list[str]]:
    """GitHub's Actions settings as TABLE, with its sub-tables, and a note on
    each setting left out because GitHub didn't return it or returned a value
    TABLE can't give. responses has None for an endpoint GitHub didn't give,
    whose settings are left out without a note."""
    values: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for setting in SETTINGS:
        response = responses.get(setting.endpoint)
        if response is None:
            continue
        try:
            value = setting.read(response)
        except KeyError:
            skipped[setting.key] = f"GitHub didn't return {setting.api_field}"
            continue
        if setting.problem(value) is not None:
            skipped[setting.key] = f"GitHub's value {value!r} isn't one {TABLE} can give"
            continue
        values[setting.key] = value
    if values.get("oidc.use-default-subject") is not False:
        # GitHub ignores them, and check would refuse them, so they aren't missed.
        values.pop("oidc.include-claim-keys", None)
        skipped.pop("oidc.include-claim-keys", None)
    if values.get("allowed-actions") != "selected":
        for key in [key for key in values if key.startswith("selected-actions.")]:
            del values[key]
    top: list[str] = []
    tables: dict[str, list[str]] = {name: [] for name in SUB_TABLES}
    for key, value in values.items():
        table, _, name = key.rpartition(".")
        (tables[table] if table else top).append(f"{name} = {config.toml_value(value)}\n")
    sections = [f"{TABLE}\n" + "".join(top)]
    sections += [f"{TABLE[:-1]}.{table}]\n" + "".join(lines) for table, lines in tables.items() if lines]
    return "\n".join(sections), [f"{key}: {note}" for key, note in skipped.items()]


def dump(repo: str, token: str) -> int:
    responses: dict[Endpoint, dict[str, Any] | None] = {}
    unavailable: list[str] = []
    for endpoint in ENDPOINTS:
        if endpoint is SELECTED and not _selected_in_effect(responses.get(PERMISSIONS) or {}):
            continue
        try:
            responses[endpoint] = github.request("GET", url(repo, endpoint), token)
        except urllib.error.HTTPError as e:
            # Some settings don't apply to every repository (to a private
            # one's forks, say), and GitHub says so with a 404. Every
            # repository has the first, so a 404 there is a wrong name or
            # no access to the repository, which is an error.
            if e.code != 404 or endpoint is PERMISSIONS:
                raise
            e.close()
            responses[endpoint] = None
            unavailable.append(endpoint.path)
    text, skipped = dump_toml(responses)
    print(text, end="")
    for note in skipped:
        print(f"Left out {note}", file=sys.stderr)
    for path in unavailable:
        print(f"Left out the settings from {path}: GitHub returned 404", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils actions-settings",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`. It needs the Administration permission, "
        "and Actions for the oidc settings (write for ensure); Actions' GITHUB_TOKEN can't be given Administration.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,ensure,dump}")
    check_parser = subparsers.add_parser(
        "check", help="fail if ensure would change anything", description="Fail if ensure would change anything."
    )
    ensure_parser = subparsers.add_parser(
        "ensure", help="change settings to match", description="Change settings to match."
    )
    dump_parser = subparsers.add_parser(
        "dump",
        help="print the repository's Actions settings as a pyproject.toml table",
        description=f"Print every Actions setting the repository has as {TABLE}, to paste into pyproject.toml.",
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
        help="exit 0 even if settings need ensure (still exit 1 on an error, unlike `|| true`)",
    )
    args = parser.parse_args(argv)

    try:
        # dump doesn't read pyproject.toml, so it works before the table is there.
        settings = {} if args.mode == "dump" else load_settings()
        if not settings and args.mode != "dump":
            print(f"No Actions settings are defined: add {TABLE} to pyproject.toml.", file=sys.stderr)
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "dump":
            return dump(repo, token)
        if args.mode == "check":
            return check(repo, token, settings, exit_zero=args.exit_zero)
        return ensure(repo, token, settings)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        hints = {
            404: "GitHub returns 404 for a setting that doesn't apply to the repository, "
            "or a repository the token can't see.",
            409: "GitHub refuses a change an organization policy forbids with 409.",
        }
        hint = f"\n({hints[e.code]})" if e.code in hints else ""
        print(f"HTTP {e.code} from {e.filename}:\n{detail}{hint}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1


run = main

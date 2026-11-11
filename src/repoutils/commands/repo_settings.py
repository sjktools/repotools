"""Check that a repository's general settings match pyproject.toml, or change
them so they do.

``[tool.repoutils.repo-settings]`` covers the description and homepage, and
the Features and Pull Requests sections of the repository's General settings
page: which features are on, who can open pull requests, which merge methods
are allowed and their default commit messages, auto-merge, branch updates,
head branch deletion and web commit sign-off. A setting the table leaves out
is left as it is on GitHub.

It deliberately can't change the repository's name, owner, default branch or
visibility, archive it or make it a template: the table rejects those keys,
and ensure never sends them.

``ensure`` changes every setting that differs, in one request, and fails if
GitHub doesn't apply one. ``check`` fails exactly when ``ensure`` would change
something. Stdlib-only, like every repoutils command."""

import argparse
import os
import subprocess
import sys
import urllib.error
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoutils import config, github

TABLE = "[tool.repoutils.repo-settings]"


@dataclass(frozen=True)
class Setting:
    # Its key in TABLE, with a dot for a nested table's key ("squash-merge.title").
    key: str
    # The repository's REST API field.
    api_field: str
    # The value's type: bool, or str (for an enum, see choices).
    kind: type
    # For an enum, each config value and the API value it stands for.
    choices: Mapping[str, str] | None = field(default=None, compare=False)

    def to_config(self, value: Any) -> Any:
        """GitHub's value as pyproject.toml would write it."""
        if self.choices is not None:
            return {api: ours for ours, api in self.choices.items()}.get(value, value)
        if self.kind is str and value is None:
            # GitHub returns null for an empty description or homepage.
            return ""
        return value

    def to_api(self, value: Any) -> Any:
        return self.choices[value] if self.choices is not None else value


SETTINGS = [
    Setting("description", "description", str),
    Setting("homepage", "homepage", str),
    # Features.
    Setting("issues", "has_issues", bool),
    Setting("projects", "has_projects", bool),
    Setting("wiki", "has_wiki", bool),
    Setting("discussions", "has_discussions", bool),
    Setting("downloads", "has_downloads", bool),
    Setting("pull-requests", "has_pull_requests", bool),
    Setting(
        "pull-request-creation",
        "pull_request_creation_policy",
        str,
        {"all": "all", "collaborators": "collaborators_only"},
    ),
    # Pull Requests.
    Setting("merge-commit.allow", "allow_merge_commit", bool),
    Setting(
        "merge-commit.title", "merge_commit_title", str, {"pr-title": "PR_TITLE", "merge-message": "MERGE_MESSAGE"}
    ),
    Setting(
        "merge-commit.message",
        "merge_commit_message",
        str,
        {"pr-title": "PR_TITLE", "pr-body": "PR_BODY", "blank": "BLANK"},
    ),
    Setting("squash-merge.allow", "allow_squash_merge", bool),
    Setting(
        "squash-merge.title",
        "squash_merge_commit_title",
        str,
        {"pr-title": "PR_TITLE", "commit-or-pr-title": "COMMIT_OR_PR_TITLE"},
    ),
    Setting(
        "squash-merge.message",
        "squash_merge_commit_message",
        str,
        {"pr-body": "PR_BODY", "commit-messages": "COMMIT_MESSAGES", "blank": "BLANK"},
    ),
    Setting("rebase-merge.allow", "allow_rebase_merge", bool),
    Setting("allow-auto-merge", "allow_auto_merge", bool),
    Setting("allow-update-branch", "allow_update_branch", bool),
    Setting("delete-branch-on-merge", "delete_branch_on_merge", bool),
    Setting("require-web-commit-signoff", "web_commit_signoff_required", bool),
]
BY_KEY = {setting.key: setting for setting in SETTINGS}
# GitHub needs a commit title whenever it's sent the matching message.
PAIRS = [("merge_commit_title", "merge_commit_message"), ("squash_merge_commit_title", "squash_merge_commit_message")]
# Keys someone might reach for that this command deliberately doesn't manage.
REFUSED = {
    "name": "the repository's name",
    "owner": "the repository's owner",
    "default-branch": "the default branch",
    "private": "the repository's visibility",
    "visibility": "the repository's visibility",
    "archived": "archiving",
    "is-template": "whether it's a template",
    "template": "whether it's a template",
}


def load_settings(root: Path = Path()) -> dict[Setting, Any]:
    """Each setting ``root/pyproject.toml`` gives, and its value as pyproject.toml
    writes it, raising config.ConfigError (a ValueError) if the table is malformed."""
    table = config.section("repo-settings", root)
    flat: dict[str, Any] = {}
    for key, value in table.items():
        if isinstance(value, dict):
            flat.update({f"{key}.{sub}": subvalue for sub, subvalue in value.items()})
        else:
            flat[key] = value
    settings: dict[Setting, Any] = {}
    for key, value in flat.items():
        if key in REFUSED:
            raise config.ConfigError(f"{TABLE} can't set {key}: repoutils doesn't manage {REFUSED[key]}")
        setting = BY_KEY.get(key)
        if setting is None:
            raise config.ConfigError(f"{TABLE} has an unknown setting: {key}")
        if setting.choices is not None:
            if not isinstance(value, str) or value not in setting.choices:
                choices = ", ".join(f'"{choice}"' for choice in setting.choices)
                raise config.ConfigError(f"{TABLE} {key} must be one of {choices}")
        elif type(value) is not setting.kind:
            kind = "true or false" if setting.kind is bool else "a string"
            raise config.ConfigError(f"{TABLE} {key} must be {kind}")
        settings[setting] = value
    return settings


@dataclass
class Plan:
    """What ensure changes: the API fields it sends, and a note on each setting that differs."""

    fields: dict[str, Any] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)


def plan(settings: dict[Setting, Any], existing: dict[str, Any]) -> Plan:
    """What ensure does to make existing (GitHub's repository) match settings."""
    missing = [setting.api_field for setting in settings if setting.api_field not in existing]
    if missing:
        raise RuntimeError(
            f"GitHub didn't return {', '.join(missing)}: the token may not have access to the repository's settings"
        )
    result = Plan()
    for setting, ours in settings.items():
        theirs = setting.to_config(existing[setting.api_field])
        if theirs != ours:
            result.fields[setting.api_field] = setting.to_api(ours)
            result.changes.append(f"{setting.key} is {theirs!r} on GitHub, not {ours!r}")
    for title, message in PAIRS:
        if message in result.fields:
            result.fields.setdefault(title, existing[title])
    return result


def repo_url(repo: str) -> str:
    return f"{github.API_URL}/repos/{repo}"


def check(repo: str, token: str, settings: dict[Setting, Any], *, exit_zero: bool) -> int:
    p = plan(settings, github.request("GET", repo_url(repo), token))
    for change in p.changes:
        print(change)
    if p.changes:
        print(f"{len(p.changes)} of {len(settings)} setting(s) in {repo} need `repoutils repo-settings ensure`.")
        return 0 if exit_zero else 1
    print(f"All {len(settings)} setting(s) in {repo} match pyproject.toml.")
    return 0


def ensure(repo: str, token: str, settings: dict[Setting, Any]) -> int:
    p = plan(settings, github.request("GET", repo_url(repo), token))
    if p.fields:
        updated = github.request("PATCH", repo_url(repo), token, json_body=p.fields)
        # GitHub ignores a field it doesn't accept (or a value the owner's
        # policy forbids) instead of refusing the request, so check each.
        unapplied = plan(settings, updated).changes
        for change in p.changes:
            print(f"updated: {change}")
        if unapplied:
            for change in unapplied:
                print(f"GitHub didn't apply the change: {change}", file=sys.stderr)
            return 1
    print(f"Updated {len(p.changes)} of {len(settings)} setting(s) in {repo}.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils repo-settings",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`; ensure needs a token that can administer "
        "the repository, which Actions' GITHUB_TOKEN can't be given.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,ensure}")
    check_parser = subparsers.add_parser(
        "check", help="fail if ensure would change anything", description="Fail if ensure would change anything."
    )
    ensure_parser = subparsers.add_parser(
        "ensure", help="change settings to match", description="Change settings to match."
    )
    for sub in (check_parser, ensure_parser):
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
        settings = load_settings()
        if not settings:
            print(f"No repository settings are defined: add {TABLE} to pyproject.toml.", file=sys.stderr)
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "check":
            return check(repo, token, settings, exit_zero=args.exit_zero)
        return ensure(repo, token, settings)
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

"""Check that a repository's security settings match pyproject.toml, or change
them so they do.

``[tool.repoutils.security-settings]`` covers the settings on the repository's
Advanced Security settings page that GitHub's REST API can read and change:
private vulnerability reporting, Dependabot alerts and security updates,
CodeQL default setup, secret protection and push protection. Each is ``true``
or ``false``. A setting the table leaves out is left as it is on GitHub.

``ensure`` changes every setting that differs, then reads them all back and
fails if GitHub didn't apply one (as when an organization's policy controls
it). ``check`` fails exactly when ``ensure`` would change something.

``dump`` prints every setting the repository has as the table, to paste into
pyproject.toml (``check`` then passes) and edit. Stdlib-only, like every
repoutils command."""

import argparse
import os
import subprocess
import sys
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoutils import config, github

TABLE = "[tool.repoutils.security-settings]"
# How long ensure waits for GitHub to finish turning on CodeQL default setup.
CODEQL_WAIT_SECONDS = 120
CODEQL_POLL_SECONDS = 10


def repo_url(repo: str) -> str:
    return f"{github.API_URL}/repos/{repo}"


def _get_or_404(url: str, token: str) -> bool:
    """Whether GET url succeeds (GitHub's 204), or False if it's a 404:
    how GitHub reports a setting that's off on some endpoints."""
    try:
        github.request("GET", url, token)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        e.close()
        return False
    return True


def _security_and_analysis(repo: str, token: str) -> dict[str, Any]:
    """The repository's security_and_analysis object, which GitHub only returns
    to a token that can administer the repository."""
    found = github.request("GET", repo_url(repo), token).get("security_and_analysis")
    if not isinstance(found, dict):
        raise RuntimeError(
            f"GitHub didn't return {repo}'s security settings: the token needs to be able to administer the repository"
        )
    return found


def _status(field_name: str) -> Callable[[dict[str, Any]], bool]:
    """A reader of field_name's status in security_and_analysis."""

    def read(analysis: dict[str, Any]) -> bool:
        status = (analysis.get(field_name) or {}).get("status")
        if status not in ("enabled", "disabled"):
            raise RuntimeError(f"GitHub returned {status!r} as the status of {field_name}, not enabled or disabled")
        return bool(status == "enabled")

    return read


def _status_writer(field_name: str) -> Callable[[str, str, bool], Any]:
    def write(repo: str, token: str, on: bool) -> Any:
        body = {"security_and_analysis": {field_name: {"status": "enabled" if on else "disabled"}}}
        return github.request("PATCH", repo_url(repo), token, json_body=body)

    return write


def _toggle_writer(path: str) -> Callable[[str, str, bool], Any]:
    """A writer for an endpoint that PUT turns on and DELETE turns off."""

    def write(repo: str, token: str, on: bool) -> Any:
        return github.request("PUT" if on else "DELETE", f"{repo_url(repo)}/{path}", token)

    return write


def _read_private_reporting(repo: str, token: str) -> bool:
    enabled = github.request("GET", f"{repo_url(repo)}/private-vulnerability-reporting", token).get("enabled")
    if not isinstance(enabled, bool):
        raise RuntimeError(f"GitHub returned {enabled!r} for private vulnerability reporting, not true or false")
    return enabled


def _read_codeql(repo: str, token: str) -> bool:
    state = github.request("GET", f"{repo_url(repo)}/code-scanning/default-setup", token).get("state")
    if state not in ("configured", "not-configured"):
        raise RuntimeError(f"GitHub returned {state!r} as CodeQL default setup's state")
    return bool(state == "configured")


def _write_codeql(repo: str, token: str, on: bool) -> Any:
    body = {"state": "configured" if on else "not-configured"}
    return github.request("PATCH", f"{repo_url(repo)}/code-scanning/default-setup", token, json_body=body)


@dataclass(frozen=True)
class Setting:
    # Its key in TABLE.
    key: str
    # The label GitHub's Advanced Security page gives it.
    label: str
    # Reads it from security_and_analysis, for a setting that's there.
    from_analysis: Callable[[dict[str, Any]], bool] | None
    # Reads it with a request of its own, for a setting that isn't.
    from_api: Callable[[str, str], bool] | None
    # Turns it on (true) or off.
    write: Callable[[str, str, bool], Any]


# In the order ensure turns them on, and the reverse of the order it turns them
# off: Dependabot security updates need Dependabot alerts, and push protection
# needs secret protection.
SETTINGS = [
    Setting(
        "private-vulnerability-reporting",
        "Private vulnerability reporting",
        None,
        _read_private_reporting,
        _toggle_writer("private-vulnerability-reporting"),
    ),
    Setting(
        "dependabot-alerts",
        "Dependabot alerts",
        None,
        lambda repo, token: _get_or_404(f"{repo_url(repo)}/vulnerability-alerts", token),
        _toggle_writer("vulnerability-alerts"),
    ),
    Setting(
        "dependabot-security-updates",
        "Dependabot security updates",
        _status("dependabot_security_updates"),
        None,
        _toggle_writer("automated-security-fixes"),
    ),
    Setting("codeql-default-setup", "CodeQL analysis (default setup)", None, _read_codeql, _write_codeql),
    Setting(
        "secret-protection", "Secret Protection", _status("secret_scanning"), None, _status_writer("secret_scanning")
    ),
    Setting(
        "push-protection",
        "Push protection",
        _status("secret_scanning_push_protection"),
        None,
        _status_writer("secret_scanning_push_protection"),
    ),
]
BY_KEY = {setting.key: setting for setting in SETTINGS}
# Each setting that GitHub only turns on with another on.
NEEDS = {"dependabot-security-updates": "dependabot-alerts", "push-protection": "secret-protection"}
# Settings on the page that GitHub's REST API can't read or change, which the
# table rejects so nobody thinks repoutils checks them.
UNSUPPORTED = {
    "dependency-graph": "GitHub's API can't turn the dependency graph on or off on its own "
    "(turning on dependabot-alerts turns it on)",
    "automatic-dependency-submission": "GitHub's API can't read or change automatic dependency submission",
    "malware-alerts": "GitHub's API can't read or change Dependabot malware alerts",
    "grouped-security-updates": "GitHub's API can't read or change grouped security updates",
    "copilot-autofix": "GitHub's API can't read or change Copilot Autofix",
}


def load_settings(root: Path = Path()) -> dict[Setting, bool]:
    """Each setting ``root/pyproject.toml`` gives and its value, raising
    config.ConfigError (a ValueError) if the table is malformed."""
    settings: dict[Setting, bool] = {}
    for key, value in config.section("security-settings", root).items():
        if key in UNSUPPORTED:
            raise config.ConfigError(f"{TABLE} can't set {key}: {UNSUPPORTED[key]}")
        setting = BY_KEY.get(key)
        if setting is None:
            raise config.ConfigError(f"{TABLE} has an unknown setting: {key}")
        if not isinstance(value, bool):
            raise config.ConfigError(f"{TABLE} {key} must be true or false")
        settings[setting] = value
    for dependent, needed in NEEDS.items():
        if settings.get(BY_KEY[dependent]) is True and settings.get(BY_KEY[needed]) is False:
            # GitHub can't do both, so ensure would always fail.
            raise config.ConfigError(f"{TABLE} {dependent} can't be true while {needed} is false")
    return settings


def read(repo: str, token: str, wanted: list[Setting]) -> dict[Setting, bool]:
    """The value each wanted setting has on GitHub. It always reads
    security_and_analysis first, so a token that can't administer the
    repository fails, rather than its 404s reading as settings that are off."""
    analysis = _security_and_analysis(repo, token)
    values: dict[Setting, bool] = {}
    for setting in wanted:
        if setting.from_analysis is not None:
            values[setting] = setting.from_analysis(analysis)
        else:
            assert setting.from_api is not None
            values[setting] = setting.from_api(repo, token)
    return values


def differences(settings: dict[Setting, bool], existing: dict[Setting, bool]) -> list[str]:
    return [
        f"{setting.key} is {_toml(existing[setting])} on GitHub, not {_toml(ours)}"
        for setting, ours in settings.items()
        if existing[setting] != ours
    ]


def _toml(value: bool) -> str:
    return "true" if value else "false"


def dump_toml(values: dict[Setting, bool]) -> str:
    lines = [f"{TABLE}\n"]
    for setting in SETTINGS:
        if setting in values:
            lines.append(f"# {setting.label}\n{setting.key} = {_toml(values[setting])}\n")
    return "".join(lines)


def dump(repo: str, token: str) -> int:
    values: dict[Setting, bool] = {}
    skipped: list[str] = []
    analysis = _security_and_analysis(repo, token)
    for setting in SETTINGS:
        if setting.from_analysis is not None:
            values[setting] = setting.from_analysis(analysis)
            continue
        assert setting.from_api is not None
        try:
            values[setting] = setting.from_api(repo, token)
        except urllib.error.HTTPError as e:
            # A setting the repository's plan doesn't offer (such as CodeQL on a
            # private repository without GitHub Code Security) is refused.
            with e:
                skipped.append(f"{setting.key}: GitHub refused to read it (HTTP {e.code})")
    print(dump_toml(values), end="")
    for note in skipped:
        print(f"Left out {note}", file=sys.stderr)
    return 0


def check(repo: str, token: str, settings: dict[Setting, bool], *, exit_zero: bool) -> int:
    changes = differences(settings, read(repo, token, list(settings)))
    for change in changes:
        print(change)
    if changes:
        print(f"{len(changes)} of {len(settings)} setting(s) in {repo} need `repoutils security-settings ensure`.")
        return 0 if exit_zero else 1
    print(f"All {len(settings)} setting(s) in {repo} match pyproject.toml.")
    return 0


def _read_back(repo: str, token: str, settings: dict[Setting, bool], codeql_started: bool) -> list[str]:
    """What still differs once ensure's changes are made. GitHub may finish
    turning on CodeQL default setup only once the run it starts for it is
    done, so ensure waits a while for that."""
    codeql = BY_KEY["codeql-default-setup"]
    deadline = time.monotonic() + CODEQL_WAIT_SECONDS
    while True:
        existing = read(repo, token, list(settings))
        waiting = codeql_started and existing[codeql] != settings[codeql]
        if not waiting or time.monotonic() >= deadline:
            return differences(settings, existing)
        time.sleep(CODEQL_POLL_SECONDS)


def ensure(repo: str, token: str, settings: dict[Setting, bool]) -> int:
    existing = read(repo, token, list(settings))
    changed = [setting for setting, ours in settings.items() if existing[setting] != ours]
    turn_off = [setting for setting in reversed(SETTINGS) if setting in changed and not settings[setting]]
    turn_on = [setting for setting in SETTINGS if setting in changed and settings[setting]]
    codeql_started = False
    for setting in turn_off + turn_on:
        response = setting.write(repo, token, settings[setting])
        if setting.key == "codeql-default-setup" and isinstance(response, dict) and response.get("run_url"):
            codeql_started = True
            print(f"GitHub started CodeQL default setup: {response['run_url']}")
        print(f"updated: {setting.key} is {_toml(existing[setting])} on GitHub, not {_toml(settings[setting])}")
    if changed:
        unapplied = _read_back(repo, token, settings, codeql_started)
        if unapplied:
            for change in unapplied:
                print(f"GitHub didn't apply the change: {change}", file=sys.stderr)
            print(
                "The repository's plan may not offer it, or an organization's security configuration may control it.",
                file=sys.stderr,
            )
            return 1
    print(f"Updated {len(changed)} of {len(settings)} setting(s) in {repo}.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils security-settings",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`; it needs a token that can administer the "
        "repository, which Actions' GITHUB_TOKEN can't be given.",
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
        help="print the repository's security settings as a pyproject.toml table",
        description=f"Print every setting the repository has as {TABLE}, to paste into pyproject.toml.",
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
            print(f"No security settings are defined: add {TABLE} to pyproject.toml.", file=sys.stderr)
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
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1


run = main

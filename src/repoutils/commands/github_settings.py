"""Run every GitHub settings command in one go.

``check`` and ``ensure`` run each GitHub settings command whose table
``pyproject.toml`` has, and say which they skip.
``dump`` runs every command, as dump doesn't read ``pyproject.toml``.

``secrets`` has no ``ensure`` mode, as repoutils can't know secret values, so
``ensure`` skips it.
``ensure`` stops at the first command that fails, so that, for example, a
deploy key's secret isn't set in an environment whose protection didn't apply.
Stdlib-only, like every repoutils command."""

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from repoutils import config, github
from repoutils.commands import (
    actions_settings,
    deploy_keys,
    environments,
    labels,
    repo_settings,
    rulesets,
    secrets,
    security_settings,
)


def _has_table(name: str) -> Callable[[dict[str, Any]], bool]:
    return lambda table: name in table


def _has_secrets(table: dict[str, Any]) -> bool:
    # [tool.repoutils.secrets] with only `other` is for `secrets check-workflows`, which `secrets check` refuses.
    listed = table.get("secrets")
    return isinstance(listed, dict) and any(key in listed for key in ("actions", "dependabot", "environments"))


@dataclass(frozen=True)
class Command:
    name: str
    run: Callable[[list[str]], int]
    # Whether [tool.repoutils] configures the command, so that `check` and `ensure` have something to do.
    configured: Callable[[dict[str, Any]], bool]
    has_ensure: bool = True
    has_fail_on_unexpected: bool = False


# In the order ensure must run: environments before the deploy keys whose secrets they hold.
COMMANDS = [
    Command("labels", labels.main, _has_table("labels"), has_fail_on_unexpected=True),
    Command("repo-settings", repo_settings.main, _has_table("repo-settings")),
    Command("rulesets", rulesets.main, _has_table("rulesets"), has_fail_on_unexpected=True),
    Command("environments", environments.main, _has_table("environments"), has_fail_on_unexpected=True),
    Command("actions-settings", actions_settings.main, _has_table("actions-settings")),
    Command("secrets", secrets.main, _has_secrets, has_ensure=False, has_fail_on_unexpected=True),
    Command("deploy-keys", deploy_keys.main, _has_table("deploy-keys"), has_fail_on_unexpected=True),
    Command("security-settings", security_settings.main, _has_table("security-settings")),
]


def _command_args(command: Command, mode: str, repo: str, *, exit_zero: bool, fail_on_unexpected: bool) -> list[str]:
    args = [mode, "--repo", repo]
    if mode == "check" and exit_zero:
        args.append("--exit-zero")
    if mode == "check" and fail_on_unexpected and command.has_fail_on_unexpected:
        args.append("--fail-on-unexpected")
    return args


def _selected(mode: str, table: dict[str, Any]) -> list[Command]:
    selected = []
    for command in COMMANDS:
        if mode == "ensure" and not command.has_ensure:
            print(f"Skipped {command.name}: it has no ensure.", file=sys.stderr)
        elif mode != "dump" and not command.configured(table):
            print(f"Skipped {command.name}: pyproject.toml doesn't configure it.", file=sys.stderr)
        else:
            selected.append(command)
    return selected


@contextmanager
def _token(token: str) -> Iterator[None]:
    """GITHUB_TOKEN set to token while the commands run, so they don't each ask gh for it."""
    old = os.environ.get("GITHUB_TOKEN")
    os.environ["GITHUB_TOKEN"] = token
    try:
        yield
    finally:
        if old is None:
            del os.environ["GITHUB_TOKEN"]
        else:
            os.environ["GITHUB_TOKEN"] = old


def _run(commands: list[Command], mode: str, repo: str, *, exit_zero: bool, fail_on_unexpected: bool) -> int:
    failed = []
    for i, command in enumerate(commands):
        if mode == "dump":
            if i:
                print()
        else:
            print(f"== repoutils {command.name} {mode}", flush=True)
        args = _command_args(command, mode, repo, exit_zero=exit_zero, fail_on_unexpected=fail_on_unexpected)
        if command.run(args):
            failed.append(command.name)
            if mode == "ensure":
                skipped = [c.name for c in commands[i + 1 :]]
                if skipped:
                    print(
                        f"Stopped after {command.name} failed, without running {', '.join(skipped)}.", file=sys.stderr
                    )
                break
    if failed:
        print(f"Failed: {', '.join(failed)}.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils github-settings",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,ensure,dump}")
    check_parser = subparsers.add_parser(
        "check",
        help="run the check of every GitHub settings command pyproject.toml configures",
        description="Run the check of every GitHub settings command pyproject.toml configures.",
    )
    subparsers.add_parser(
        "ensure",
        help="run the ensure of every GitHub settings command pyproject.toml configures, except secrets",
        description="Run the ensure of every GitHub settings command pyproject.toml configures, except secrets, "
        "stopping at the first that fails.",
    )
    subparsers.add_parser(
        "dump",
        help="run the dump of every GitHub settings command",
        description="Run the dump of every GitHub settings command.",
    )
    for sub in subparsers.choices.values():
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
    check_parser.add_argument(
        "--fail-on-unexpected",
        action="store_true",
        help="pass --fail-on-unexpected to the commands that have it",
    )
    args = parser.parse_args(argv)

    try:
        table = {} if args.mode == "dump" else config.load()
        commands = _selected(args.mode, table)
        if not commands:
            print("pyproject.toml configures none of the GitHub settings commands.", file=sys.stderr)
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    with _token(token):
        return _run(
            commands,
            args.mode,
            repo,
            exit_zero=getattr(args, "exit_zero", False),
            fail_on_unexpected=getattr(args, "fail_on_unexpected", False),
        )


run = main

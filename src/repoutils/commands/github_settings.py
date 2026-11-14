"""Run every GitHub settings command in one go.

``check`` runs each GitHub settings command's ``check`` mode.
``dump`` runs each command's ``dump`` mode.
``ensure`` runs each command's ``ensure`` mode where that mode exists.

``secrets`` has no ``ensure`` mode, as secret values can't be read back from
GitHub, so ``ensure`` skips it.
Stdlib-only, like every repoutils command."""

import argparse
import os
from collections.abc import Callable
from dataclasses import dataclass

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


@dataclass(frozen=True)
class Command:
    name: str
    run: Callable[[list[str]], int]
    has_ensure: bool = True


COMMANDS = [
    Command("labels", labels.main),
    Command("repo-settings", repo_settings.main),
    Command("rulesets", rulesets.main),
    Command("environments", environments.main),
    Command("actions-settings", actions_settings.main),
    Command("secrets", secrets.main, has_ensure=False),
    Command("deploy-keys", deploy_keys.main),
    Command("security-settings", security_settings.main),
]


def _command_args(mode: str, *, repo: str | None, exit_zero: bool) -> list[str]:
    args = [mode]
    if repo:
        args += ["--repo", repo]
    if mode == "check" and exit_zero:
        args.append("--exit-zero")
    return args


def _run(mode: str, *, repo: str | None, exit_zero: bool) -> int:
    failures = 0
    for command in COMMANDS:
        if mode == "ensure" and not command.has_ensure:
            continue
        result = command.run(_command_args(mode, repo=repo, exit_zero=exit_zero))
        if result:
            failures += 1
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils github-settings",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,ensure,dump}")
    check_parser = subparsers.add_parser(
        "check", help="run every GitHub settings check", description="Run every GitHub settings check."
    )
    for name in ("ensure", "dump"):
        subparsers.add_parser(
            name,
            help=f"run every GitHub settings {name}" + (" (except secrets)" if name == "ensure" else ""),
            description=f"Run every GitHub settings {name}" + (" (except secrets)." if name == "ensure" else "."),
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
    args = parser.parse_args(argv)
    return _run(args.mode, repo=args.repo, exit_zero=getattr(args, "exit_zero", False))


run = main

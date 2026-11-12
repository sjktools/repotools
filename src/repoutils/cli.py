"""The ``repoutils`` command: ``repoutils <command> [args...]``.

Each command is a module in ``repoutils.commands`` with a ``run(argv) -> int``
function, which parses its own arguments. Modules are imported only when their
command runs, so ``repoutils --help`` stays fast.
"""

import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version


@dataclass(frozen=True)
class Command:
    name: str
    module: str
    summary: str


GROUPS: dict[str, list[Command]] = {
    "Repo hygiene": [
        Command("check-whitespace", "check_whitespace", "Check Git-tracked text files for trailing whitespace"),
        Command(
            "check-sentence-per-line", "check_sentence_per_line", "Check Markdown files keep one sentence per line"
        ),
        Command(
            "check-prose",
            "check_prose",
            "Check Markdown prose for long, clause-heavy sentences, or too many short ones",
        ),
        Command(
            "check-blame-ignore-revs", "check_blame_ignore_revs", "Check .git-blame-ignore-revs lists real commits"
        ),
        Command("check-screenshots-dir", "check_screenshots_dir", "Check only .gitignore is tracked in screenshots/"),
        Command("git-config-setup", "git_config_setup", "Set the repo's local Git config (blame.ignoreRevsFile)"),
        Command("long-files", "long_files", "List files over the line limit, or post that to a PR"),
    ],
    "GitHub Actions workflows": [
        Command("check-pins", "check_pins", "Check workflows pin the project's uv and just versions"),
        Command("check-checkout-credentials", "check_checkout_credentials", "Check checkouts set persist-credentials"),
        Command("claude-route", "claude_route", "Decide which Claude workflow a comment (on stdin) is for"),
    ],
    "uv and Dependabot": [
        Command(
            "requirements-min-versions", "requirements_min_versions", "Check or fix dependency minimums vs uv.lock"
        ),
        Command("dependabot-relock", "dependabot_relock", "Re-lock a Dependabot branch's uv.lock with this uv"),
        Command("check-lock-platforms", "check_lock_platforms", "Check uv.lock only has wheels for allowed platforms"),
    ],
    "PR reporting": [
        Command("post-comment", "post_comment", "Post a Markdown file to a PR, editing or hiding the last one"),
        Command("post-patch-coverage", "post_patch_coverage", "Post a diff-cover patch coverage report to a PR"),
        Command("wait-for-base-coverage", "wait_for_base_coverage", "Wait for Codecov to have a PR base's coverage"),
        Command("post-screenshots", "post_screenshots", "Post screenshots to a PR as a comment"),
        Command("recreate-pr", "recreate_pr", "Recreate a PR as a draft and close the original"),
    ],
    "GitHub repository": [
        Command("labels", "labels", "Check or create the issue and PR labels pyproject.toml defines"),
        Command("repo-settings", "repo_settings", "Check or change the repository settings pyproject.toml gives"),
        Command("rulesets", "rulesets", "Check, create or update the branch rulesets pyproject.toml defines"),
    ],
    "Releases": [
        Command("check-attestations", "check_attestations", "Check every distribution has a PEP 740 attestation"),
    ],
}

COMMANDS = {command.name: command for commands in GROUPS.values() for command in commands}


def _version() -> str:
    try:
        return version("repoutils")
    except PackageNotFoundError:  # pragma: no cover - only when run from an uninstalled checkout
        return "unknown"


def usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = ["usage: repoutils <command> [args...]", "", "Commands:"]
    for group, commands in GROUPS.items():
        lines += ["", f"  {group}:"]
        lines += [f"    {command.name:<{width}}  {command.summary}" for command in commands]
    lines += ["", "Run `repoutils <command> --help` for a command's options."]
    return "\n".join(lines)


def load(name: str) -> Callable[[list[str]], int]:
    """The ``run`` function of the named command."""
    run: Callable[[list[str]], int] = importlib.import_module(f"repoutils.commands.{COMMANDS[name].module}").run
    return run


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help", "help"):
        print(usage())
        return 0 if args else 2
    if args[0] == "--version":
        print(f"repoutils {_version()}")
        return 0
    if args[0] not in COMMANDS:
        print(f"repoutils: unknown command {args[0]!r}\n\n{usage()}", file=sys.stderr)
        return 2
    return load(args[0])(args[1:])

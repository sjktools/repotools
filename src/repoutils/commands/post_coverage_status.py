"""Post a commit's coverage as two commit statuses: the whole project's, from
``coverage.xml``, and the patch's, from diff-cover's JSON report of the lines
a PR (or, outside a PR, the commit) changes.

Both statuses (``coverage/project`` and ``coverage/patch`` unless
``[tool.repoutils.coverage]`` names them otherwise) are always ``success``:
they record coverage and don't judge it. Their descriptions hold the figures in
the formats ``repoutils.coverage_status`` documents. Given ``--base``, the
project status also gives the change from that commit's project status, if it
has one, so the default branch's statuses keep a history a PR can compare with.
"""

import argparse
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

from repoutils import coverage_status, github


def _sha(value: str) -> str:
    """value if it's a full commit SHA, which GitHub needs to post a status on."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError(f"not a full commit SHA: {value!r}")
    return value


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils post-coverage-status",
        description="Post the project coverage in a coverage.xml, and the patch coverage in a "
        "diff-cover JSON report, as commit statuses, always 'success', with the figures in their "
        "descriptions. Uses $GITHUB_TOKEN (which needs statuses: write), defaulting to `gh auth token`.",
    )
    parser.add_argument("commit", type=_sha, help="full SHA of the commit the reports are for")
    parser.add_argument("--report", type=Path, default=Path("coverage.xml"), help="default: coverage.xml")
    parser.add_argument(
        "--patch-report",
        type=Path,
        help="diff-cover's JSON report (--format json:<file>); without it, no patch status is posted",
    )
    parser.add_argument(
        "--base",
        type=_sha,
        help="commit to compare with: the PR's base, or the commit's parent; the project status gives "
        "the change from its project status, if it has one",
    )
    parser.add_argument("--run-url", help="URL of the workflow run that measured it, which the statuses link to")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the statuses it would post, and post nothing")
    args = parser.parse_args(argv)

    try:
        # ConfigError is a ValueError, so a bad setting fails here too, even when main() is called directly.
        project_name, patch_name = coverage_status.status_names()
        totals = coverage_status.read_report(args.report)
        patch = None if args.patch_report is None else coverage_status.read_patch_report(args.patch_report)
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1

    patch_status = [] if patch is None else [(patch_name, coverage_status.patch_description(patch))]
    if args.dry_run and not args.base:
        for name, description in [(project_name, totals.description()), *patch_status]:
            print(f"{name}: {description}")
        return 0

    try:
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        base = None
        if args.base:
            base = coverage_status.read_status(repo, args.base, project_name, token)
            if base is None:
                print(f"{args.base} has no {project_name} status, so the change from it isn't shown.")
        project = coverage_status.project_description(totals, base, args.base or "")
        for name, description in [(project_name, project), *patch_status]:
            if args.dry_run:
                print(f"{name}: {description}")
            else:
                coverage_status.post_status(repo, args.commit, name, description, token, args.run_url)
                print(f"Posted {name}: {description} on {args.commit}.")
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError) as e:
        # ValueError: the base's status isn't in the format this posts.
        print(e, file=sys.stderr)
        return 1
    return 0


run = main

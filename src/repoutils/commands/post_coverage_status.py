"""Post a project's coverage, from its ``coverage.xml``, as a commit status.

The status (``coverage/project`` unless ``[tool.repoutils.coverage]
status-name`` says otherwise) is always ``success``: it records coverage and
doesn't judge it. Its description holds the figure in the fixed format
``repoutils.coverage_status`` documents, so the default branch's run posting
one per commit keeps a history, and a PR can read its base commit's figure.
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
        description="Post the whole-project coverage in a coverage.xml as a commit status, "
        "always 'success', with the figure in its description. Posts with $GITHUB_TOKEN "
        "(which needs statuses: write), defaulting to `gh auth token`.",
    )
    parser.add_argument("commit", type=_sha, help="full SHA of the commit the report is for")
    parser.add_argument("--report", type=Path, default=Path("coverage.xml"), help="default: coverage.xml")
    parser.add_argument("--run-url", help="URL of the workflow run that measured it, which the status links to")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the status it would post, and post nothing")
    args = parser.parse_args(argv)

    try:
        # ConfigError is a ValueError, so a bad setting fails here too, even when main() is called directly.
        name = coverage_status.status_name()
        totals = coverage_status.read_report(args.report)
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"{name}: {totals.description()}")
        return 0

    try:
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        coverage_status.post_status(repo, args.commit, name, totals, token, args.run_url)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"Posted {name}: {totals.description()} on {args.commit}.")
    return 0


run = main

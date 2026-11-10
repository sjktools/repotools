"""Post a Markdown file to a PR as a comment, editing or hiding the one an
earlier run posted.

Each comment starts with a hidden marker, ``<!-- <comment-prefix>-<name> -->``,
where ``name`` is ``--name`` and ``comment-prefix`` comes from
``[tool.repoutils]`` (default ``repoutils``), so the next run can find it. By
default the next run edits that comment in place; with ``--hide-previous`` it
posts a new comment and hides the earlier ones as outdated instead, which suits
reports whose history is worth keeping, such as exolot's findings.

CI runs this in a job with a write-capable token that runs nothing else, so it
uses only the standard library."""

import argparse
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

from repoutils import config
from repoutils import github as github_comments


def _post(args: argparse.Namespace, marker: str) -> str:
    """Posts the comment, returning what it did."""
    body = f"{marker}\n{args.body.read_text()}"
    token = os.environ.get("GITHUB_TOKEN") or github_comments.gh_output("auth", "token")
    repo = args.repo or github_comments.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    if args.commit:
        # An older commit's run can finish after a newer one's, and mustn't
        # post over its report.
        head = github_comments.pr_head(repo, args.pr, token)
        if not head.startswith(args.commit):
            return f"Not posting: the report is for {args.commit}, but PR #{args.pr}'s head is now {head}."
    if args.hide_previous:
        # Post first, so a failure leaves the old report showing rather than none.
        posted = github_comments.request(
            "POST", f"{github_comments.API_URL}/repos/{repo}/issues/{args.pr}/comments", token, json_body={"body": body}
        )
        hidden = github_comments.hide_comments(repo, args.pr, marker, token, keep=posted["node_id"])
        return f"Posted {args.body} to PR #{args.pr}, and hid {hidden} earlier comment(s)."
    author = args.author or github_comments.request("GET", f"{github_comments.API_URL}/user", token)["login"]
    github_comments.upsert_comment(repo, args.pr, author, marker, body, token)
    return f"Posted {args.body} to PR #{args.pr}."


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils post-comment",
        description=__doc__.splitlines()[0] + " " + __doc__.splitlines()[1],
        epilog="Comments with $GITHUB_TOKEN, defaulting to `gh auth token`.",
    )
    parser.add_argument("pr", type=int, help="pull request number")
    parser.add_argument("body", type=Path, help="the Markdown file to post")
    parser.add_argument("--name", required=True, help="names the comment's marker, e.g. exolot-findings")
    parser.add_argument(
        "--hide-previous",
        action="store_true",
        help="post a new comment and hide earlier ones, rather than edit the earlier one",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument(
        "--commit",
        help="commit the report is for; if it isn't (a prefix of) the PR's head, nothing is posted",
    )
    parser.add_argument(
        "--author",
        help="login $GITHUB_TOKEN comments as, e.g. github-actions[bot], to find the comment to edit "
        "(default: looked up, which only works for a user token)",
    )
    args = parser.parse_args(argv)

    try:
        print(_post(args, config.marker(args.name)))
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
    return 0


run = main

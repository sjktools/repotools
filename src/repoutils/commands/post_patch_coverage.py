"""Post a diff-cover Markdown patch coverage report to a PR as a comment, or
update the one posted before.

Given ``--coverage-report``, the comment also shows the project's coverage, and
given ``--base`` too, the base commit's (from its project coverage status, as
``post-coverage-status`` posts it) and the change. It warns, in the comment and
as a GitHub Actions ``::warning::``, when project coverage falls by more than
``[tool.repoutils.coverage] warn-project-drop`` points, or patch coverage is
more than ``warn-patch-below`` points under the base's project coverage. It
only warns: coverage never fails the command. ``--output`` writes the comment
to a file, such as the job summary, instead of posting it, for PRs whose token
can't comment.
"""

import argparse
import os
import re
import subprocess
import sys
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from repoutils import config, coverage_status
from repoutils import github as github_comments
from repoutils.coverage_status import Totals, points

# Marks the comment this script posts, so the next run can find and edit it.
MARKER_NAME = "patch-coverage"
# The marker with the default comment-prefix; main() uses the configured one.
MARKER = f"<!-- {config.DEFAULT_COMMENT_PREFIX}-{MARKER_NAME} -->"

# GitHub rejects a comment body over 65,536 characters; leave room for the
# truncation note.
MAX_BODY_CHARS = 65_000


# The lines diff-cover's markdown_coverage_report.md template writes before
# its per-file snippets: each file as `- <path> (<pct>%)`, then
# `: Missing lines <lines>` if any are (the path's dots escaped as `&#46;` so
# GitHub doesn't link it), and the summary as `- **<name>**: <value>`. Anything
# else there fails the script, so a diff-cover upgrade that changes the format
# fails the tests (PatchCoverageRealDiffCoverTests) rather than going unnoticed.
NO_LINES = "No lines with coverage information in this diff."
_FILE_RE = re.compile(r"^- (?P<path>.+) \((?P<percent>[0-9.]+%)\)(?:: Missing lines (?P<missing>[0-9,-]+))?$")
_SUMMARY_RE = re.compile(r"^- \*\*(?P<name>Total|Missing|Coverage)\*\*: (?P<value>.+)$")
_LINE_COUNT_RE = re.compile(r"^(?P<count>\d+) lines?$")


class UnexpectedReport(ValueError):
    pass


def _code_cell(text: str) -> str:
    """text as a code span in a table cell: with its pipes escaped, as they'd
    end the cell even in a code span, and delimited by more backticks than it
    has in a row."""
    ticks = "`" * (max(map(len, re.findall("`+", text)), default=0) + 1)
    pad = " " if "`" in text else ""
    escaped = text.replace("|", "\\|")
    return f"{ticks}{pad}{escaped}{pad}{ticks}"


def _file_table(matches: list[re.Match[str]]) -> list[str]:
    rows = ["| File | Coverage | Missing lines |", "| --- | ---: | --- |"]
    for m in matches:
        path = m["path"].replace("&#46;", ".")
        # A space after each comma, so a long list can wrap.
        missing = ", ".join((m["missing"] or "").split(","))
        rows.append(f"| {_code_cell(path)} | {m['percent']} | {missing} |")
    return rows


def _summary_table(matches: list[re.Match[str]]) -> list[str]:
    return [
        "| " + " | ".join(m["name"] for m in matches) + " |",
        "|" + " ---: |" * len(matches),
        "| " + " | ".join(m["value"] for m in matches) + " |",
    ]


def _demote_headings(lines: list[str]) -> list[str]:
    """lines with each ATX heading outside a code block one level deeper."""
    demoted = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            line = "#" + line
        demoted.append(line)
    return demoted


class _Lines:
    """A cursor over a report's lines, raising UnexpectedReport for one that
    isn't what the parser expects."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.pos = 0

    def peek(self) -> str | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def fail(self, what: str) -> NoReturn:
        line = self.peek()
        found = "the end of the report" if line is None else repr(line)
        raise UnexpectedReport(f"diff-cover report line {self.pos + 1}: expected {what}, found {found}")

    def take(self, ok: Callable[[str], bool], what: str) -> str:
        """The current line, moving past it, if ok() accepts it."""
        line = self.peek()
        if line is None or not ok(line):
            self.fail(what)
        self.pos += 1
        return line

    def accept(self, text: str) -> bool:
        """Whether the current line is text, moving past it if so."""
        if self.peek() != text:
            return False
        self.pos += 1
        return True

    def match(
        self, regex: re.Pattern[str], ok: Callable[[re.Match[str]], bool] = lambda m: True
    ) -> re.Match[str] | None:
        """regex's match of the current line, moving past it, if there is one
        and ok() accepts it."""
        line = self.peek()
        m = None if line is None else regex.match(line)
        if m is None or not ok(m):
            return None
        self.pos += 1
        return m

    def skip_blank(self) -> None:
        while self.peek() == "":
            self.pos += 1

    def rest(self) -> list[str]:
        return self.lines[self.pos :]


def _reformat(report: str) -> tuple[str, list[str], Totals | None]:
    """diff-cover's report as its title, the lines after it, with its file
    list and summary as tables and its headings one level deeper, so its
    sections nest under the comment's title, and the changed lines' totals
    (None if there are none). Raises UnexpectedReport if it isn't in the shape
    diff-cover's template writes."""
    lines = _Lines(report.rstrip("\n").split("\n"))
    title = lines.take(lambda line: line == "# Diff Coverage", "'# Diff Coverage'")
    out = ["#" + lines.take(lambda line: line.startswith("## Diff: "), "'## Diff: ...'"), ""]
    lines.skip_blank()
    if lines.accept(NO_LINES):
        lines.skip_blank()
        if lines.peek() is not None:
            lines.fail("nothing more")
        return title, out + [NO_LINES], None
    files = list(iter(lambda: lines.match(_FILE_RE), None))
    if not files:
        lines.fail("a file's coverage or " + repr(NO_LINES))
    out += _file_table(files) + [""]
    lines.skip_blank()
    lines.take(lambda line: line == "## Summary", "'## Summary'")
    out += ["### Summary", ""]
    lines.skip_blank()
    summary = []
    counts = []
    for name in ("Total", "Missing", "Coverage"):
        m = lines.match(_SUMMARY_RE, lambda m: m["name"] == name)
        if m is None:
            lines.fail(f"'- **{name}**: ...'")
        if name != "Coverage":
            count = _LINE_COUNT_RE.match(m["value"])
            if count is None:
                raise UnexpectedReport(
                    f"diff-cover report line {lines.pos}: expected '<n> lines', found {m['value']!r}"
                )
            counts.append(int(count["count"]))
        summary.append(m)
    total, missing = counts
    if missing > total:
        # The Missing line, before the Coverage one just read.
        raise UnexpectedReport(f"diff-cover report line {lines.pos - 1}: {missing} missing lines of {total}")
    out += _summary_table(summary)
    # The rest is each file's snippets of missing lines, kept as they are.
    return title, out + _demote_headings(lines.rest()), Totals(total - missing, total)


@dataclass(frozen=True)
class ProjectCoverage:
    """The project's coverage at the PR's head and, given a base commit, at
    that commit (None if it has no project coverage status), with the limits
    past which the comment warns (None for no limit)."""

    head: Totals
    base_sha: str | None = None
    base: Totals | None = None
    status_name: str = coverage_status.DEFAULT_PROJECT_STATUS
    warn_project_drop: float | None = coverage_status.DEFAULT_WARN_PROJECT_DROP
    warn_patch_below: float | None = coverage_status.DEFAULT_WARN_PATCH_BELOW


def _warnings(project: ProjectCoverage, patch: Totals | None) -> list[str]:
    """What falls short of project's limits, one line each."""
    warnings = []
    if project.base is not None and project.warn_project_drop is not None:
        drop = project.base.hundredths() - project.head.hundredths()
        if drop > project.warn_project_drop * 100:
            warnings.append(
                f"Project coverage fell by {points(drop)} points from the base's; "
                f"`warn-project-drop` allows {project.warn_project_drop:g}."
            )
    if patch is not None and patch.lines_valid and project.warn_patch_below is not None:
        reference, whose = (project.base, "the base's") if project.base is not None else (project.head, "the PR's")
        shortfall = reference.hundredths() - patch.hundredths()
        if shortfall > project.warn_patch_below * 100:
            warnings.append(
                f"Patch coverage is {patch.percent_text()}%, {points(shortfall)} points below {whose} project "
                f"coverage of {reference.percent_text()}%; `warn-patch-below` allows {project.warn_patch_below:g}."
            )
    return warnings


def _project_section(project: ProjectCoverage, commit: str | None) -> list[str]:
    """The table of the project's coverage at the head and the base, and the change."""
    head = f"Head, `{commit[:7]}`" if commit else "Head"
    rows = ["### Project", "", "| Commit | Coverage |", "| --- | ---: |", f"| {head} | {project.head.description()} |"]
    if project.base_sha is None:
        return rows + [""]
    base_sha = f"`{project.base_sha[:7]}`"
    if project.base is None:
        return rows + [
            "",
            f"The base commit, {base_sha}, has no `{project.status_name}` status to compare with, "
            "as its CI failed or didn't finish in time.",
            "",
        ]
    change = project.head.hundredths() - project.base.hundredths()
    sign = "-" if change < 0 else "+"
    return rows + [
        f"| Base, {base_sha} | {project.base.description()} |",
        f"| Change | {sign}{points(abs(change))} points |",
        "",
    ]


def _build_body(
    report: str,
    commit: str | None,
    run_url: str | None,
    marker: str | None = MARKER,
    project: ProjectCoverage | None = None,
) -> tuple[str, list[str]]:
    """The comment body: diff-cover's Markdown report reformatted, after
    project's coverage and any warnings, with a line saying what it was
    measured for after its title, cut to fit a comment if need be; and the
    warnings. Raises UnexpectedReport for a report it can't reformat."""
    title, rest, patch = _reformat(report)
    head = [] if marker is None else [marker]
    head += ["## Coverage" if project else "#" + title, ""]
    if commit or run_url:
        measured = "Coverage" if project else "Patch coverage"
        if commit:
            measured += f" of {commit}"
        if run_url:
            measured += f", from [this workflow run]({run_url})"
        head += [f"{measured}.", ""]
    warnings = [] if project is None else _warnings(project, patch)
    for warning in warnings:
        head += ["> [!WARNING]", f"> {warning}", ""]
    if project is not None:
        head += _project_section(project, commit)
    body = "\n".join(head + rest)
    if len(body) <= MAX_BODY_CHARS:
        return body, warnings
    # The head always has a newline, so this never finds none.
    cut = body[: body.rfind("\n", 0, MAX_BODY_CHARS)]
    # Close a code block left open by the cut, so the note isn't inside it.
    if cut.count("\n```") % 2:
        cut += "\n```"
    where = f"[this workflow run]({run_url})" if run_url else "the workflow run"
    return f"{cut}\n\n_Report truncated; the whole of it is in the `patch-coverage` artifact of {where}._", warnings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils post-patch-coverage",
        description="Post diff-cover's Markdown patch coverage report to a PR as a "
        "comment, or edit the one posted before, with the project's coverage and its change "
        "from the PR's base if given. Coverage only ever warns, never fails it. "
        "Uses $GITHUB_TOKEN, defaulting to `gh auth token`.",
    )
    parser.add_argument("pr", type=int, nargs="?", help="pull request number (not needed with --output)")
    parser.add_argument("--report", type=Path, default=Path("patch-coverage.md"))
    parser.add_argument(
        "--coverage-report",
        type=Path,
        help="the PR's coverage.xml, to show the project's coverage too",
    )
    parser.add_argument(
        "--base",
        help="the PR's base commit, to show the change from its project coverage status (needs --coverage-report)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="append the comment to this file, e.g. $GITHUB_STEP_SUMMARY, and post nothing",
    )
    parser.add_argument(
        "--commit",
        help="commit the report is for; if it isn't (a prefix of) the PR's head, nothing is posted",
    )
    parser.add_argument("--run-url", help="URL of the workflow run that measured it")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
    )
    parser.add_argument(
        "--author",
        help="login $GITHUB_TOKEN comments as, e.g. github-actions[bot] (default: "
        "looked up, which only works for a user token)",
    )
    args = parser.parse_args(argv)
    if args.pr is None and args.output is None:
        parser.error("give a PR number, or --output")
    if args.base and args.coverage_report is None:
        parser.error("--base needs --coverage-report")

    try:
        marker = config.marker(MARKER_NAME)
        report = args.report.read_text()
        # Check the report now, so a bad one fails before anything talks to GitHub.
        _reformat(report)
        status_name, _ = coverage_status.status_names()
        limits = coverage_status.warning_limits()
        head = None if args.coverage_report is None else coverage_status.read_report(args.coverage_report)
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1

    try:
        token = ""
        repo = ""
        if args.output is None or args.base:
            token = os.environ.get("GITHUB_TOKEN") or github_comments.gh_output("auth", "token")
            repo = args.repo or github_comments.gh_output(
                "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"
            )
        author = ""
        if args.output is None:
            author = args.author or github_comments.request("GET", f"{github_comments.API_URL}/user", token)["login"]
            if args.commit:
                # As in post_screenshots: an older commit's run can finish after a
                # newer one's, and mustn't overwrite its report.
                pr_head = github_comments.pr_head(repo, args.pr, token)
                if not pr_head.startswith(args.commit):
                    print(f"Not posting: the report is for {args.commit}, but PR #{args.pr}'s head is now {pr_head}.")
                    return 0
        project = None
        if head is not None:
            base = None if not args.base else coverage_status.read_status(repo, args.base, status_name, token)
            project = ProjectCoverage(head, args.base, base, status_name, *limits)
        body, warnings = _build_body(report, args.commit, args.run_url, None if args.output else marker, project)
        for warning in warnings:
            print(f"::warning::{warning}")
        if args.output is not None:
            with args.output.open("a") as f:
                f.write(body + "\n")
            print(f"Wrote the coverage report to {args.output}.")
            return 0
        github_comments.upsert_comment(repo, args.pr, author, marker, body, token)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as e:
        # OSError: also a failed request or a file that can't be written.
        # ValueError: the base's status isn't in the format post-coverage-status posts.
        print(e, file=sys.stderr)
        return 1
    print(f"Posted the patch coverage report to PR #{args.pr}.")
    return 0


run = main

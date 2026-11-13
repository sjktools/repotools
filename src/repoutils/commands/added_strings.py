"""List the lines a PR adds that have any of the configured strings, in a Markdown report.

It prints the report, or posts it to a PR as a comment, editing the one posted
before.
The strings, and what to say about each, come from pyproject.toml's
[tool.repoutils.added-strings]. Only the lines the working tree adds since the
merge base of --base and HEAD count, not ones that were already there. It's
advisory, so finding a string never fails; a bad setting, a failed git command
or a failed request to GitHub still does. Stdlib-only, as CI runs it with a
write-capable token without installing the project."""

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoutils import config as repoutils_config
from repoutils import github as github_comments

# Marks the comment this posts, so the next run can find and edit it. This is
# the marker with the default comment-prefix; main() uses the configured one.
MARKER_NAME = "added-strings"
MARKER = f"<!-- {repoutils_config.DEFAULT_COMMENT_PREFIX}-{MARKER_NAME} -->"

TABLE = "[tool.repoutils.added-strings]"
# The locations listed for each string; the rest are counted.
MAX_LOCATIONS = 20
# Characters of a matching line shown in the report.
MAX_TEXT = 120


@dataclass(frozen=True)
class Rule:
    # What's looked for: a plain substring, or a regular expression.
    pattern: str
    is_regex: bool
    # What the report says about it, as Markdown, or None for the default.
    message: str | None
    include: list[str] = field(default_factory=lambda: ["*"])
    exclude: list[str] = field(default_factory=list)

    def matches(self, text: str) -> bool:
        return bool(re.search(self.pattern, text)) if self.is_regex else self.pattern in text

    def covers(self, path: str) -> bool:
        return _covers(path, self.include, self.exclude)

    def describe(self) -> str:
        """What the PR adds, for the report: "`x`", or "lines matching `x`"."""
        return f"lines matching {code_span(self.pattern)}" if self.is_regex else code_span(self.pattern)

    def advice(self) -> str:
        if self.message is not None:
            return self.message
        if self.is_regex:
            return "please double check if each is actually necessary"
        return f"please double check if use of {code_span(self.pattern)} is actually necessary"


@dataclass(frozen=True)
class Config:
    rules: list[Rule]
    include: list[str] = field(default_factory=lambda: ["*"])
    exclude: list[str] = field(default_factory=list)

    def covers(self, path: str) -> bool:
        return _covers(path, self.include, self.exclude)


@dataclass(frozen=True)
class Location:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class Finding:
    rule: Rule
    locations: list[Location]


def _covers(path: str, include: list[str], exclude: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in include) and not any(fnmatch.fnmatchcase(path, p) for p in exclude)


def _rule(entry: object, index: int) -> Rule:
    where = f"{TABLE} strings[{index}]"
    if not isinstance(entry, dict):
        raise repoutils_config.ConfigError(f"{where} must be a table")
    unknown = sorted(set(entry) - {"string", "regex", "message", "include", "exclude"})
    if unknown:
        raise repoutils_config.ConfigError(f"{where} has unknown keys: {', '.join(unknown)}")
    if ("string" in entry) == ("regex" in entry):
        raise repoutils_config.ConfigError(f"{where} must have one of string or regex")
    is_regex = "regex" in entry
    pattern = entry["regex" if is_regex else "string"]
    if not isinstance(pattern, str) or not pattern:
        raise repoutils_config.ConfigError(f"{where} {'regex' if is_regex else 'string'} must be a non-empty string")
    if is_regex:
        try:
            re.compile(pattern)
        except re.error as error:
            raise repoutils_config.ConfigError(f"{where} regex {pattern!r} is invalid: {error}") from error
    message = entry.get("message")
    if message is not None and not (isinstance(message, str) and message):
        raise repoutils_config.ConfigError(f"{where} message must be a non-empty string")
    return Rule(
        pattern=pattern,
        is_regex=is_regex,
        message=message,
        include=repoutils_config.string_list(entry, "include", ["*"], where),
        exclude=repoutils_config.string_list(entry, "exclude", [], where),
    )


def load_config(root: Path) -> Config:
    """[tool.repoutils.added-strings] from root's pyproject.toml, raising
    ConfigError if it's malformed. With no table, there's nothing to warn about."""
    table: dict[str, Any] = repoutils_config.section("added-strings", root)
    unknown = sorted(set(table) - {"strings", "include", "exclude"})
    if unknown:
        raise repoutils_config.ConfigError(f"{TABLE} has unknown keys: {', '.join(unknown)}")
    entries = table.get("strings", [])
    if not isinstance(entries, list):
        raise repoutils_config.ConfigError(f"{TABLE} strings must be an array of tables")
    return Config(
        rules=[_rule(entry, i) for i, entry in enumerate(entries)],
        include=repoutils_config.string_list(table, "include", ["*"], TABLE),
        exclude=repoutils_config.string_list(table, "exclude", [], TABLE),
    )


def code_span(text: str) -> str:
    """text as a Markdown code span, whatever backticks it has."""
    longest = max((len(run) for run in re.findall("`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote(path: str) -> str:
    """A path as git prints it in a diff header, with C-style quoting undone."""
    if not (path.startswith('"') and path.endswith('"')):
        return path
    out = bytearray()
    body, i = path[1:-1], 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out += char.encode()
            i += 1
        elif body[i + 1] in _C_ESCAPES:
            out.append(_C_ESCAPES[body[i + 1]])
            i += 2
        else:
            out.append(int(body[i + 1 : i + 4], 8))
            i += 4
    return out.decode(errors="replace")


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def added_lines(cwd: Path, base: str) -> Iterator[Location]:
    """Each line the working tree adds since the merge base of base and HEAD.
    A renamed file's lines count only where the rename changes them."""
    merge_base = subprocess.run(
        ["git", "merge-base", base, "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()
    diff = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--ignore-submodules",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            merge_base,
            "--",
        ],
        cwd=cwd,
        capture_output=True,
        check=True,
    ).stdout.decode(errors="replace")
    path = ""
    line = old_left = new_left = 0
    for text in diff.split("\n"):
        if old_left or new_left:
            # A hunk's lines, which can look like headers ("+++ x" adds "++ x").
            if text.startswith("+"):
                yield Location(path, line, text[1:].rstrip("\r"))
                line += 1
                new_left -= 1
            elif text.startswith("-"):
                old_left -= 1
        elif text.startswith("+++ "):
            # "b/<path>", or /dev/null for a deleted file, which adds no lines.
            path = _unquote(text[4:].rstrip("\t")).removeprefix("b/")
        elif match := _HUNK.match(text):
            old_left = 1 if match[1] is None else int(match[1])
            line = int(match[2])
            new_left = 1 if match[3] is None else int(match[3])


def find(cwd: Path, config: Config, base: str) -> list[Finding]:
    """The configured strings the lines added since base contain, each with
    where, in the order they're configured. Strings with no matches are left out."""
    lines = [location for location in added_lines(cwd, base) if config.covers(location.path)]
    findings = []
    for rule in config.rules:
        locations = [loc for loc in lines if rule.covers(loc.path) and rule.matches(loc.text)]
        if locations:
            findings.append(Finding(rule, locations))
    return findings


def _excerpt(text: str) -> str:
    text = text.strip()
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def render(findings: list[Finding], *, base: str, commit: str | None, repo: str | None) -> str:
    """findings as the Markdown of a PR comment, without the marker."""
    link = f"https://github.com/{repo}/blob/{commit}" if repo and commit else None
    lines = ["## Strings to double check", ""]
    if not findings:
        lines.append(f"No added lines have any of the strings to double check, compared with `{base}`.")
        return "\n".join(lines) + "\n"
    lines += [
        "**For information only.** Keep each one that is actually needed, with no reply or explanation, "
        "and remove the rest.",
        "",
    ]
    if commit:
        lines += [f"Lines added at {commit}, compared with `{base}`.", ""]
    for finding in findings:
        count = len(finding.locations)
        times = "once" if count == 1 else f"{count} times"
        lines += [f"- **Warning:** PR adds {finding.rule.describe()} ({times}): {finding.rule.advice()}", ""]
        for loc in finding.locations[:MAX_LOCATIONS]:
            name = code_span(f"{loc.path}:{loc.line}")
            # ?plain=1, so a line of a Markdown file links to it rather than its rendering.
            where = f"[{name}]({link}/{urllib.parse.quote(loc.path)}?plain=1#L{loc.line})" if link else name
            excerpt = _excerpt(loc.text)
            lines.append(f"  - {where}: {code_span(excerpt)}" if excerpt else f"  - {where}")
        if count > MAX_LOCATIONS:
            lines.append(f"  - and {count - MAX_LOCATIONS} more")
        lines.append("")
    return "\n".join(lines[:-1]) + "\n"


def _post(args: argparse.Namespace, repo: str, findings: list[Finding], body: str, marker: str) -> None:
    token = os.environ.get("GITHUB_TOKEN") or github_comments.gh_output("auth", "token")
    author = args.author or github_comments.request("GET", f"{github_comments.API_URL}/user", token)["login"]
    if args.commit:
        # An older commit's run can finish after a newer one's, and mustn't
        # overwrite its report.
        head = github_comments.pr_head(repo, args.post, token)
        if not head.startswith(args.commit):
            print(f"Not posting: the report is for {args.commit}, but PR #{args.post}'s head is now {head}.")
            return
    # With nothing found, only replace an earlier report (so it doesn't
    # linger), never start a new one.
    create = bool(findings)
    if github_comments.upsert_comment(repo, args.post, author, marker, f"{marker}\n{body}", token, create=create):
        print(f"Posted the added strings report to PR #{args.post}.")
    else:
        print(f"Not posting: PR #{args.post} adds none of the strings, and has no earlier report.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils added-strings",
        description=__doc__.splitlines()[0],
        epilog="Prints the report, or with --post comments it with $GITHUB_TOKEN (default `gh auth token`). "
        "Always exits 0 when it could check, whatever it finds.",
    )
    parser.add_argument(
        "--base",
        help="branch whose merge base with HEAD the added lines are found from (default: "
        "origin/$GITHUB_BASE_REF, else origin/ and [tool.repoutils] default-branch)",
    )
    parser.add_argument(
        "--commit",
        help="commit the report is for, which links name (default: HEAD); with --post, "
        "nothing is posted unless it's (a prefix of) the PR's head",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name, for links and --post (default: $GITHUB_REPOSITORY; for --post, else gh's current repo)",
    )
    parser.add_argument("--post", type=int, metavar="PR", help="post the report to this pull request")
    parser.add_argument(
        "--author",
        help="with --post, the login $GITHUB_TOKEN comments as, e.g. github-actions[bot] (default: "
        "looked up, which only works for a user token)",
    )
    args = parser.parse_args(argv)
    cwd = Path.cwd()

    try:
        config = load_config(cwd)
        base = args.base or f"origin/{os.environ.get('GITHUB_BASE_REF') or repoutils_config.default_branch(cwd)}"
        findings = find(cwd, config, base)
        commit = (
            args.commit
            or subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
            ).stdout.strip()
        )
        repo = args.repo
        if args.post is not None and not repo:
            repo = github_comments.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        body = render(findings, base=base, commit=commit, repo=repo)
        if args.post is None:
            print(body, end="")
        else:
            assert repo
            _post(args, repo, findings, body, repoutils_config.marker(MARKER_NAME, cwd))
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else e.stderr
        print(f"{' '.join(e.cmd)} failed:\n{stderr}", file=sys.stderr)
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

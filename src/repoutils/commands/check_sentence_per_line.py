"""Check that Markdown files stay one sentence per line.

The files are ``[tool.repoutils.sentence-per-line]``'s ``files`` (default
``AGENTS.md`` and ``README.md``); a missing one is skipped.

A file that can't be read (one that isn't valid UTF-8, for example) is
reported, and fails the check.

Outside code fences, tables, headings and HTML lines, this flags sentence
boundaries that appear in the middle of one line.
"""

import argparse
import re
from pathlib import Path

from repoutils import config
from repoutils.markdown import INLINE_CODE_RE, UnreadableError, read_text, unfenced_lines

DEFAULT_FILES = ["AGENTS.md", "README.md"]
ABBREVIATIONS = ("e.g.", "i.e.", "etc.")
# An abbreviation as a whole word, at the end of the text before a boundary.
ABBREVIATION_RE = re.compile(r"(?<![\w.])(?:" + "|".join(map(re.escape, ABBREVIATIONS)) + r")$", re.IGNORECASE)
# An ordered-list marker, optionally after bullet or blockquote markers (e.g. "- 1." or "> 1.").
LIST_MARKER_RE = re.compile(r"\s*(?:[-*+>]\s*)*\d+\.")
BOUNDARY_RE = re.compile(r'[.!?][)"*_’”]*[ \t]+[A-Z`“‘\[\(\*]')


def _mask_inline_code(line: str) -> str:
    # Blank out code spans' contents but keep their backticks, so a sentence starting with code is still seen.
    return INLINE_CODE_RE.sub(lambda m: m[1] + " " * len(m[2]) + m[1], line)


def _has_boundary(line: str) -> bool:
    masked = _mask_inline_code(line)
    for match in BOUNDARY_RE.finditer(masked):
        boundary = match.start()
        if line[boundary] == "." and boundary > 0 and line[boundary - 1] == ".":
            continue
        if line[boundary] == "." and LIST_MARKER_RE.fullmatch(line[: boundary + 1]):
            continue
        if line[boundary] == "." and ABBREVIATION_RE.search(line[: boundary + 1]):
            continue
        return True
    return False


def _line_ignored(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("#"):
        return True
    if stripped.startswith("|"):
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    return False


def _problems(text: str) -> list[tuple[int, str]]:
    return [(number, line) for number, line in unfenced_lines(text) if not _line_ignored(line) and _has_boundary(line)]


def files() -> list[str]:
    """The files to check, from config."""
    table = config.section("sentence-per-line")
    return config.string_list(table, "files", DEFAULT_FILES, "[tool.repoutils.sentence-per-line]")


def main() -> int:
    paths = [path for path in files() if Path(path).is_file()]
    failures = []
    for path in paths:
        try:
            text = read_text(path)
        except UnreadableError as error:
            failures.append(str(error))
            continue
        for number, line in _problems(text):
            failures.append(f"{path}:{number}: sentence boundary mid-line: {line}")
    if failures:
        print("One-sentence-per-line check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"{', '.join(paths) or 'No files'} follow one-sentence-per-line formatting.")
    return 0


def run(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="repoutils check-sentence-per-line", description=__doc__.splitlines()[0]).parse_args(
        argv
    )
    return main()

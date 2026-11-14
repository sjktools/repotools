"""A commit's coverage as two commit statuses: the whole project's, read from
a Cobertura ``coverage.xml``, and the patch's (the lines a PR or commit
changes), read from diff-cover's JSON report. This module writes their
descriptions, parses the project one back, and posts or reads the statuses.

The project status's description has one fixed format, so anything can parse
it::

    96.52% (2114/2172 lines, 610/650 branches), +0.12 points from 1a2b3c4

The branches part is there only when the report measured branches, and the
change from a base commit only when that commit has a project status. The
percentage is coverage.py's total, covered lines and branches over all of
them, cut (not rounded) to two decimal places, so it never shows 100.00% when
something is missed. The change is the difference between the two figures as
shown.

The patch status reads the same, with lines only, as diff-cover counts them,
or ``no changed lines to cover``. Standard library only.
"""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from repoutils import config, github

DEFAULT_PROJECT_STATUS = "coverage/project"
DEFAULT_PATCH_STATUS = "coverage/patch"
NO_PATCH_LINES = "no changed lines to cover"

_DESCRIPTION_RE = re.compile(
    r"^(?P<totals>(?P<percent>\d{1,3}\.\d{2})% \((?P<lines_covered>\d+)/(?P<lines_valid>\d+) lines"
    r"(?:, (?P<branches_covered>\d+)/(?P<branches_valid>\d+) branches)?\))"
    r"(?:, [+-]\d+\.\d{2} points from [0-9a-f]{7,40})?$"
)


@dataclass(frozen=True)
class Totals:
    """A whole project's coverage counts."""

    lines_covered: int
    lines_valid: int
    branches_covered: int = 0
    branches_valid: int = 0

    @property
    def percent(self) -> float:
        """Covered lines and branches as a percentage of all of them, as coverage.py totals them."""
        return 100 * (self.lines_covered + self.branches_covered) / (self.lines_valid + self.branches_valid)

    def hundredths(self) -> int:
        """percent in hundredths of a point, cut rather than rounded."""
        return 10_000 * (self.lines_covered + self.branches_covered) // (self.lines_valid + self.branches_valid)

    def percent_text(self) -> str:
        """percent to two decimal places, cut rather than rounded."""
        return _points(self.hundredths())

    def description(self) -> str:
        """The status description, e.g. ``96.52% (2114/2172 lines, 610/650 branches)``."""
        counts = f"{self.lines_covered}/{self.lines_valid} lines"
        if self.branches_valid:
            counts += f", {self.branches_covered}/{self.branches_valid} branches"
        return f"{self.percent_text()}% ({counts})"


def _points(hundredths: int) -> str:
    return f"{hundredths // 100}.{hundredths % 100:02d}"


def project_description(totals: Totals, base: Totals | None = None, base_sha: str = "") -> str:
    """The project status's description, with the change from base's figure
    (as both are shown) if there is one, e.g. ``96.52% (2114/2172 lines),
    -0.10 points from 1a2b3c4``."""
    if base is None:
        return totals.description()
    change = totals.hundredths() - base.hundredths()
    sign = "-" if change < 0 else "+"
    return f"{totals.description()}, {sign}{_points(abs(change))} points from {base_sha[:7]}"


def patch_description(totals: Totals) -> str:
    """The patch status's description, e.g. ``92.30% (12/13 lines)``."""
    return totals.description() if totals.lines_valid else NO_PATCH_LINES


def _count(root: ET.Element, name: str, path: Path) -> int:
    value = root.get(name)
    if value is None:
        raise ValueError(f"{path}: the report has no {name}")
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f"{path}: {name} is {value!r}, not a whole number")
    return int(value)


def read_report(path: Path) -> Totals:
    """The totals in a Cobertura XML report, such as coverage.py's
    ``coverage.xml``, raising OSError if it can't be read and ValueError if
    it isn't such a report or measured nothing."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise ValueError(f"{path}: not valid XML ({error})") from None
    if root.tag != "coverage" or "lines-valid" not in root.attrib:
        raise ValueError(f"{path}: not a Cobertura coverage report")
    totals = Totals(*(_count(root, name, path) for name in ("lines-covered", "lines-valid")))
    # A report without branch data has neither branch count.
    if "branches-valid" in root.attrib or "branches-covered" in root.attrib:
        totals = Totals(
            totals.lines_covered,
            totals.lines_valid,
            _count(root, "branches-covered", path),
            _count(root, "branches-valid", path),
        )
    if totals.lines_valid + totals.branches_valid == 0:
        raise ValueError(f"{path}: the report measured no lines")
    if totals.lines_covered > totals.lines_valid or totals.branches_covered > totals.branches_valid:
        raise ValueError(f"{path}: the report covers more lines or branches than it measured")
    return totals


def _whole(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def read_patch_report(path: Path) -> Totals:
    """The changed lines' totals in a diff-cover JSON report, raising OSError
    if it can't be read and ValueError if it isn't such a report."""
    try:
        report = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{path}: not valid JSON ({error})") from None
    lines = report.get("total_num_lines") if isinstance(report, dict) else None
    violations = report.get("total_num_violations") if isinstance(report, dict) else None
    if not (_whole(lines) and _whole(violations)):
        raise ValueError(f"{path}: not a diff-cover JSON report (no total_num_lines and total_num_violations)")
    if violations > lines:
        raise ValueError(f"{path}: the report misses more lines than it measured")
    return Totals(lines - violations, lines)


def parse_description(description: str) -> Totals:
    """The totals a project status description gives, raising ValueError if it
    isn't in the format ``project_description`` writes."""
    m = _DESCRIPTION_RE.match(description)
    if m is None:
        raise ValueError(f"not a coverage status description: {description!r}")
    totals = Totals(
        int(m["lines_covered"]),
        int(m["lines_valid"]),
        int(m["branches_covered"] or 0),
        int(m["branches_valid"] or 0),
    )
    if (
        totals.lines_valid + totals.branches_valid == 0
        or totals.lines_covered > totals.lines_valid
        or totals.branches_covered > totals.branches_valid
        or totals.description() != m["totals"]
    ):
        raise ValueError(f"coverage status description doesn't add up: {description!r}")
    return totals


def _status_name(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    # GitHub limits a status's context to 255 characters.
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise config.ConfigError(
            f"[tool.repoutils.coverage] {key} must be a non-empty string of 255 characters at most"
        )
    return value


def status_names(root: Path = Path()) -> tuple[str, str]:
    """The project and patch statuses' names (``[tool.repoutils.coverage]``
    ``project-status`` and ``patch-status``, default ``coverage/project`` and
    ``coverage/patch``)."""
    table = config.section("coverage", root)
    project = _status_name(table, "project-status", DEFAULT_PROJECT_STATUS)
    patch = _status_name(table, "patch-status", DEFAULT_PATCH_STATUS)
    # GitHub matches a status's context ignoring case.
    if project.casefold() == patch.casefold():
        raise config.ConfigError("[tool.repoutils.coverage] project-status and patch-status must differ")
    return project, patch


def post_status(repo: str, sha: str, name: str, description: str, token: str, target_url: str | None = None) -> None:
    """Post a successful status called name on commit sha, which a later one
    with the same name replaces. It's always ``success``, as it records
    coverage and doesn't judge it."""
    body: dict[str, str] = {"state": "success", "context": name, "description": description}
    if target_url:
        body["target_url"] = target_url
    github.request("POST", f"{github.API_URL}/repos/{repo}/statuses/{sha}", token, json_body=body)


def latest_status(repo: str, sha: str, name: str, token: str) -> dict[str, Any] | None:
    """The latest status called name on commit sha, or None if it has none."""
    page = 1
    while True:
        combined = github.request(
            "GET", f"{github.API_URL}/repos/{repo}/commits/{sha}/status?per_page=100&page={page}", token
        )
        statuses: list[dict[str, Any]] = combined["statuses"]
        # The combined status holds only the latest status for each name, which GitHub matches ignoring case.
        for status in statuses:
            if status["context"].casefold() == name.casefold():
                return status
        if len(statuses) < 100:
            return None
        page += 1


def read_status(repo: str, sha: str, name: str, token: str) -> Totals | None:
    """The totals commit sha's status called name records, or None if it has
    no such status. Raises ValueError if it has one this module didn't write."""
    status = latest_status(repo, sha, name, token)
    if status is None:
        return None
    return parse_description(status.get("description") or "")

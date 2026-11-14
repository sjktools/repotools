"""A project's coverage as a commit status: its totals read from a Cobertura
``coverage.xml``, the status description that records them, and posting that
status on a commit or reading it back.

The description has one fixed format, so anything can parse it::

    96.52% (2114/2172 lines, 610/650 branches)

The branches part is there only when the report measured branches. The
percentage is coverage.py's total, covered lines and branches over all of
them, cut (not rounded) to two decimal places, so it never shows 100.00% when
something is missed. Standard library only.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from repoutils import config, github

DEFAULT_STATUS_NAME = "coverage/project"

_DESCRIPTION_RE = re.compile(
    r"^(?P<percent>\d{1,3}\.\d{2})% \((?P<lines_covered>\d+)/(?P<lines_valid>\d+) lines"
    r"(?:, (?P<branches_covered>\d+)/(?P<branches_valid>\d+) branches)?\)$"
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

    def percent_text(self) -> str:
        """percent to two decimal places, cut rather than rounded."""
        hundredths = 10_000 * (self.lines_covered + self.branches_covered) // (self.lines_valid + self.branches_valid)
        return f"{hundredths // 100}.{hundredths % 100:02d}"

    def description(self) -> str:
        """The status description, e.g. ``96.52% (2114/2172 lines, 610/650 branches)``."""
        counts = f"{self.lines_covered}/{self.lines_valid} lines"
        if self.branches_valid:
            counts += f", {self.branches_covered}/{self.branches_valid} branches"
        return f"{self.percent_text()}% ({counts})"


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


def parse_description(description: str) -> Totals:
    """The totals a status description gives, raising ValueError if it isn't
    in the format ``Totals.description`` writes."""
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
        or totals.description() != description
    ):
        raise ValueError(f"coverage status description doesn't add up: {description!r}")
    return totals


def status_name(root: Path = Path()) -> str:
    """The commit status's name (``[tool.repoutils.coverage] status-name``,
    default ``coverage/project``)."""
    value = config.section("coverage", root).get("status-name", DEFAULT_STATUS_NAME)
    # GitHub limits a status's context to 255 characters.
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise config.ConfigError(
            "[tool.repoutils.coverage] status-name must be a non-empty string of 255 characters at most"
        )
    return value


def post_status(repo: str, sha: str, name: str, totals: Totals, token: str, target_url: str | None = None) -> None:
    """Post totals on commit sha as a successful status called name, which a
    later one with the same name replaces. It's always ``success``, as it
    records coverage and doesn't judge it."""
    body: dict[str, str] = {"state": "success", "context": name, "description": totals.description()}
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
        # The combined status holds only the latest status for each name.
        for status in statuses:
            if status["context"] == name:
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

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from repoutils import config, coverage_status
from repoutils.coverage_status import Totals


def _xml(**attrs: str) -> str:
    attributes = " ".join(f'{name.replace("_", "-")}="{value}"' for name, value in attrs.items())
    return f'<?xml version="1.0" ?>\n<coverage version="7" {attributes}><packages/></coverage>\n'


@pytest.mark.parametrize(
    ("totals", "description"),
    [
        (Totals(2114, 2172, 610, 650), "96.52% (2114/2172 lines, 610/650 branches)"),
        (Totals(2114, 2172), "97.32% (2114/2172 lines)"),
        (Totals(10, 10, 4, 4), "100.00% (10/10 lines, 4/4 branches)"),
        # Cut, not rounded, so a miss never shows as 100.00%.
        (Totals(99_999, 100_000), "99.99% (99999/100000 lines)"),
        (Totals(0, 3), "0.00% (0/3 lines)"),
        (Totals(1, 3), "33.33% (1/3 lines)"),
        (Totals(2, 3), "66.66% (2/3 lines)"),
    ],
)
def test_description_round_trips(totals: Totals, description: str) -> None:
    assert totals.description() == description
    assert coverage_status.parse_description(description) == totals


def test_percent() -> None:
    assert Totals(3, 4, 1, 4).percent == 50.0


@pytest.mark.parametrize(
    "description",
    [
        "",
        "97.31%",
        "97.3% (2114/2172 lines)",
        "97.31% (2114/2172 lines) ",
        "97.31% (2114/2172 lines, 1/2)",
        "50.00% (1/2 lines), +1.00 points",
        "50.00% (1/2 lines), 1.00 points from 1a2b3c4",
        # Doesn't add up.
        "50.00% (2114/2172 lines)",
        "1.00% (0/0 lines)",
        "100.00% (5/4 lines)",
        # Adds up overall, but covers more lines than it measured.
        "100.00% (2/1 lines, 0/1 branches)",
    ],
)
def test_parse_rejects(description: str) -> None:
    with pytest.raises(ValueError, match="description"):
        coverage_status.parse_description(description)


def test_read_report_with_branches(tmp_path: Path) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text(_xml(lines_valid="20", lines_covered="15", branches_valid="10", branches_covered="5"))
    assert coverage_status.read_report(path) == Totals(15, 20, 5, 10)


def test_read_report_without_branches(tmp_path: Path) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text(_xml(lines_valid="20", lines_covered="15"))
    assert coverage_status.read_report(path) == Totals(15, 20)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("<coverage", "not valid XML"),
        ("<report/>", "not a Cobertura coverage report"),
        ("<coverage/>", "not a Cobertura coverage report"),
        (_xml(lines_valid="2", lines_covered="x"), "lines-covered is 'x', not a whole number"),
        (_xml(lines_valid="-1", lines_covered="0"), "lines-valid is '-1'"),
        (_xml(lines_valid="2", lines_covered="1", branches_valid="2", branches_covered="1.5"), "branches-covered"),
        (_xml(lines_valid="2"), "has no lines-covered"),
        (_xml(lines_valid="2", lines_covered="1", branches_valid="2"), "has no branches-covered"),
        (_xml(lines_valid="2", lines_covered="1", branches_covered="2"), "has no branches-valid"),
        (_xml(lines_valid="0", lines_covered="0"), "measured no lines"),
        (_xml(lines_valid="2", lines_covered="3"), "covers more"),
        (_xml(lines_valid="2", lines_covered="2", branches_valid="1", branches_covered="2"), "covers more"),
    ],
)
def test_read_report_rejects(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        coverage_status.read_report(path)


def test_read_report_missing(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        coverage_status.read_report(tmp_path / "coverage.xml")


def test_read_real_coverage_report(tmp_path: Path) -> None:
    """What coverage.py itself writes parses, with the total its own report gives."""
    (tmp_path / "mod.py").write_text("def f(x):\n    if x:\n        return 1\n    return 2\n\n\nf(True)\n")

    def coverage(*args: str) -> str:
        # In a subprocess, so it doesn't disturb this run's own coverage.
        command = [sys.executable, "-m", "coverage", *args]
        return subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True).stdout

    coverage("run", "--branch", "--data-file=.cov", "mod.py")
    coverage("xml", "--data-file=.cov", "-o", "coverage.xml")
    total = float(coverage("report", "--data-file=.cov", "--format=total", "--precision=4"))
    totals = coverage_status.read_report(tmp_path / "coverage.xml")
    assert totals == Totals(4, 5, 1, 2)
    assert totals.percent == pytest.approx(total, abs=1e-4)


def test_status_names(tmp_path: Path) -> None:
    assert coverage_status.status_names(tmp_path) == ("coverage/project", "coverage/patch")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.repoutils.coverage]\nproject-status = "cov"\npatch-status = "cov/diff"\n'
    )
    assert coverage_status.status_names(tmp_path) == ("cov", "cov/diff")


@pytest.mark.parametrize("key", ["project-status", "patch-status"])
@pytest.mark.parametrize("value", ["3", '""', '" "', '"' + "x" * 256 + '"'])
def test_bad_status_name(tmp_path: Path, key: str, value: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[tool.repoutils.coverage]\n{key} = {value}\n")
    with pytest.raises(config.ConfigError, match=f"{key} must be a non-empty string"):
        coverage_status.status_names(tmp_path)


def test_same_status_names(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.repoutils.coverage]\npatch-status = "coverage/project"\n')
    with pytest.raises(config.ConfigError, match="project-status and patch-status must differ"):
        coverage_status.status_names(tmp_path)


BASE_SHA = "1a2b3c4d" + "0" * 32


@pytest.mark.parametrize(
    ("totals", "base", "description"),
    [
        (Totals(1, 2), None, "50.00% (1/2 lines)"),
        (Totals(3, 4), Totals(1, 2), "75.00% (3/4 lines), +25.00 points from 1a2b3c4"),
        (Totals(1, 3), Totals(1, 2), "33.33% (1/3 lines), -16.67 points from 1a2b3c4"),
        (Totals(1, 2), Totals(2, 4), "50.00% (1/2 lines), +0.00 points from 1a2b3c4"),
        # The change between the figures as shown, so it adds up for a reader.
        (Totals(2, 3), Totals(1, 3), "66.66% (2/3 lines), +33.33 points from 1a2b3c4"),
    ],
)
def test_project_description(totals: Totals, base: Totals | None, description: str) -> None:
    assert coverage_status.project_description(totals, base, BASE_SHA) == description
    assert coverage_status.parse_description(description) == totals


@pytest.mark.parametrize(
    ("totals", "description"),
    [(Totals(12, 13), "92.30% (12/13 lines)"), (Totals(0, 0), "no changed lines to cover")],
)
def test_patch_description(totals: Totals, description: str) -> None:
    assert coverage_status.patch_description(totals) == description


def test_read_patch_report(tmp_path: Path) -> None:
    path = tmp_path / "patch.json"
    path.write_text('{"total_num_lines": 13, "total_num_violations": 1, "src_stats": {}}')
    assert coverage_status.read_patch_report(path) == Totals(12, 13)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{", "not valid JSON"),
        ("[]", "not a diff-cover JSON report"),
        ('{"total_num_lines": 1}', "not a diff-cover JSON report"),
        ('{"total_num_lines": true, "total_num_violations": 0}', "not a diff-cover JSON report"),
        ('{"total_num_lines": -1, "total_num_violations": 0}', "not a diff-cover JSON report"),
        ('{"total_num_lines": 1, "total_num_violations": 2}', "misses more lines"),
    ],
)
def test_read_patch_report_rejects(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "patch.json"
    path.write_text(text)
    with pytest.raises(ValueError, match=message):
        coverage_status.read_patch_report(path)


def test_read_real_patch_report(tmp_path: Path) -> None:
    """What the installed diff-cover writes parses."""

    def run(*args: str | Path) -> None:
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)

    git = ("git", "-c", "user.name=t", "-c", "user.email=t@example.com")
    run(*git, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("a = 1\n")
    run(*git, "add", "a.py")
    run(*git, "commit", "-qm", "base")
    (tmp_path / "a.py").write_text("a = 1\nb = 2\nc = 3\n")
    run(*git, "commit", "-qam", "change")
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0" ?><coverage version="7"><sources><source>.</source></sources>'
        '<packages><package name="."><classes><class name="a.py" filename="a.py"><lines>'
        '<line number="1" hits="1"/><line number="2" hits="1"/><line number="3" hits="0"/>'
        "</lines></class></classes></package></packages></coverage>"
    )
    diff_cover = Path(sys.executable).parent / "diff-cover"
    run(diff_cover, "coverage.xml", "--compare-branch=HEAD^", "--format", "json:patch.json")
    assert coverage_status.read_patch_report(tmp_path / "patch.json") == Totals(1, 2)


def test_post_status() -> None:
    with mock.patch("repoutils.github.request") as request:
        coverage_status.post_status("o/r", "abc", "cov", "50.00% (1/2 lines)", "tok", "https://run")
        coverage_status.post_status("o/r", "abc", "cov", "50.00% (1/2 lines)", "tok")
    assert request.call_args_list == [
        mock.call(
            "POST",
            "https://api.github.com/repos/o/r/statuses/abc",
            "tok",
            json_body={
                "state": "success",
                "context": "cov",
                "description": "50.00% (1/2 lines)",
                "target_url": "https://run",
            },
        ),
        mock.call(
            "POST",
            "https://api.github.com/repos/o/r/statuses/abc",
            "tok",
            json_body={"state": "success", "context": "cov", "description": "50.00% (1/2 lines)"},
        ),
    ]


def _fake_statuses(pages: list[list[dict[str, Any]]], urls: list[str]) -> Any:
    def fake_request(method: str, url: str, token: str) -> Any:
        assert method == "GET"
        urls.append(url)
        return {"statuses": pages[len(urls) - 1]}

    return fake_request


def test_read_status() -> None:
    urls: list[str] = []
    other = [{"context": "ci", "description": "ok"}] * 100
    pages = [other, [{"context": "coverage/project", "description": "50.00% (1/2 lines)"}]]
    with mock.patch("repoutils.github.request", _fake_statuses(pages, urls)):
        assert coverage_status.read_status("o/r", "abc", "coverage/project", "tok") == Totals(1, 2)
    assert urls == [
        "https://api.github.com/repos/o/r/commits/abc/status?per_page=100&page=1",
        "https://api.github.com/repos/o/r/commits/abc/status?per_page=100&page=2",
    ]


def test_read_status_missing() -> None:
    urls: list[str] = []
    with mock.patch("repoutils.github.request", _fake_statuses([[{"context": "ci", "description": None}]], urls)):
        assert coverage_status.read_status("o/r", "abc", "coverage/project", "tok") is None
    assert len(urls) == 1


def test_read_status_not_ours() -> None:
    urls: list[str] = []
    with (
        mock.patch("repoutils.github.request", _fake_statuses([[{"context": "cov", "description": None}]], urls)),
        pytest.raises(ValueError, match="not a coverage status description: ''"),
    ):
        coverage_status.read_status("o/r", "abc", "cov", "tok")

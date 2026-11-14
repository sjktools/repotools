import io
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from repoutils.commands.post_coverage_status import main

SHA = "0123456789abcdef0123456789abcdef01234567"
BASE = "fedcba9876543210fedcba9876543210fedcba98"
REPORT = '<coverage lines-valid="4" lines-covered="3" branches-valid="2" branches-covered="1"/>'
PATCH = '{"total_num_lines": 3, "total_num_violations": 1}'
STATUSES_URL = f"https://api.github.com/repos/o/r/statuses/{SHA}"


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    report: str | None = REPORT,
    base_statuses: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> tuple[int, list[tuple[Any, ...]]]:
    """Run main() in tmp_path, holding coverage.xml (unless report is None)
    and patch.json, with the API faked: the base commit has base_statuses.
    Return the exit code and each request as (method, url, token, json_body)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs")
    if report is not None:
        (tmp_path / "coverage.xml").write_text(report)
    (tmp_path / "patch.json").write_text(PATCH)
    requests: list[tuple[Any, ...]] = []

    def fake_request(method: str, url: str, token: str, *, json_body: object = None) -> Any:
        requests.append((method, url, token, json_body))
        if error:
            raise error
        if method == "GET":
            assert url.startswith(f"https://api.github.com/repos/o/r/commits/{BASE}/status?")
            return {"statuses": base_statuses or []}
        return None

    with mock.patch("repoutils.github.request", fake_request):
        return main([SHA, "--repo", "o/r", *args]), requests


def _posted(requests: list[tuple[Any, ...]]) -> list[tuple[str, str]]:
    return [(r[3]["context"], r[3]["description"]) for r in requests if r[0] == "POST"]


def test_posts_project_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, requests = _run(tmp_path, monkeypatch, "--run-url", "https://run")
    assert code == 0
    assert requests == [
        (
            "POST",
            STATUSES_URL,
            "ghs",
            {
                "state": "success",
                "context": "coverage/project",
                "description": "66.66% (3/4 lines, 1/2 branches)",
                "target_url": "https://run",
            },
        )
    ]
    assert capsys.readouterr().out == f"Posted coverage/project: 66.66% (3/4 lines, 1/2 branches) on {SHA}.\n"


def test_posts_both_with_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_statuses = [{"context": "coverage/project", "description": "50.00% (1/2 lines)"}]
    code, requests = _run(
        tmp_path, monkeypatch, "--patch-report", "patch.json", "--base", BASE, base_statuses=base_statuses
    )
    assert code == 0
    assert _posted(requests) == [
        ("coverage/project", "66.66% (3/4 lines, 1/2 branches), +16.66 points from fedcba9"),
        ("coverage/patch", "66.66% (2/3 lines)"),
    ]


def test_base_without_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, requests = _run(tmp_path, monkeypatch, "--base", BASE)
    assert code == 0
    assert _posted(requests) == [("coverage/project", "66.66% (3/4 lines, 1/2 branches)")]
    assert f"{BASE} has no coverage/project status" in capsys.readouterr().out


def test_base_status_not_ours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base_statuses = [{"context": "coverage/project", "description": "great"}]
    code, requests = _run(tmp_path, monkeypatch, "--base", BASE, base_statuses=base_statuses)
    assert (code, _posted(requests)) == (1, [])
    assert "not a coverage status description: 'great'" in capsys.readouterr().err


def test_configured_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.repoutils.coverage]\nproject-status = "cov/total"\npatch-status = "cov/diff"\n'
    )
    code, requests = _run(tmp_path, monkeypatch, "--patch-report", "patch.json")
    assert code == 0
    assert [name for name, _ in _posted(requests)] == ["cov/total", "cov/diff"]
    assert all("target_url" not in r[3] for r in requests)


def test_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code, requests = _run(tmp_path, monkeypatch, "--dry-run", "--patch-report", "patch.json")
    assert (code, requests) == (0, [])
    assert capsys.readouterr().out == (
        "coverage/project: 66.66% (3/4 lines, 1/2 branches)\ncoverage/patch: 66.66% (2/3 lines)\n"
    )


def test_dry_run_with_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    base_statuses = [{"context": "coverage/project", "description": "100.00% (2/2 lines)"}]
    code, requests = _run(tmp_path, monkeypatch, "--dry-run", "--base", BASE, base_statuses=base_statuses)
    assert code == 0
    assert [r[0] for r in requests] == ["GET"]
    assert capsys.readouterr().out == "coverage/project: 66.66% (3/4 lines, 1/2 branches), -33.34 points from fedcba9\n"


@pytest.mark.parametrize(
    ("args", "report", "message"),
    [
        ((), None, "No such file"),
        ((), "<coverage/>", "not a Cobertura"),
        (("--patch-report", "missing.json"), REPORT, "missing.json"),
    ],
)
def test_bad_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
    report: str | None,
    message: str,
) -> None:
    code, requests = _run(tmp_path, monkeypatch, *args, report=report)
    assert (code, requests) == (1, [])
    assert message in capsys.readouterr().err


def test_bad_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.repoutils.coverage]\nproject-status = 3\n")
    code, requests = _run(tmp_path, monkeypatch)
    assert (code, requests) == (1, [])
    assert "project-status must be a non-empty string" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["main"], [SHA, "--base", "HEAD^"]])
def test_bad_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        main(args)
    assert exit_info.value.code == 2
    assert "not a full commit SHA" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            urllib.error.HTTPError("https://x", 403, "Forbidden", Message(), io.BytesIO(b"nope")),
            "HTTP 403 from https://x:\nnope",
        ),
        (urllib.error.URLError("down"), "down"),
    ],
)
def test_request_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: Exception, message: str
) -> None:
    code, _ = _run(tmp_path, monkeypatch, error=error)
    assert code == 1
    assert message in capsys.readouterr().err


def test_gh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / "coverage.xml").write_text(REPORT)
    failure = subprocess.CalledProcessError(1, ["gh", "auth", "token"], stderr="not logged in")
    with mock.patch("repoutils.github.gh_output", side_effect=failure):
        assert main([SHA]) == 1
    assert "gh auth token failed:\nnot logged in" in capsys.readouterr().err

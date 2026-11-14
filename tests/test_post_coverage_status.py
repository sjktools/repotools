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
REPORT = '<coverage lines-valid="4" lines-covered="3" branches-valid="2" branches-covered="1"/>'


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    report: str | None = REPORT,
    error: Exception | None = None,
) -> tuple[int, list[tuple[Any, ...]]]:
    """Run main() in tmp_path, holding coverage.xml unless report is None,
    with the API faked. Return the exit code and each request as (method,
    url, token, json_body)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs")
    if report is not None:
        (tmp_path / "coverage.xml").write_text(report)
    requests: list[tuple[Any, ...]] = []

    def fake_request(method: str, url: str, token: str, *, json_body: object = None) -> None:
        requests.append((method, url, token, json_body))
        if error:
            raise error

    with mock.patch("repoutils.github.request", fake_request):
        return main([SHA, "--repo", "o/r", *args]), requests


def test_posts_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, requests = _run(tmp_path, monkeypatch, "--run-url", "https://run")
    assert code == 0
    assert requests == [
        (
            "POST",
            f"https://api.github.com/repos/o/r/statuses/{SHA}",
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


def test_configured_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text('[tool.repoutils.coverage]\nstatus-name = "cov/total"\n')
    code, requests = _run(tmp_path, monkeypatch)
    assert code == 0
    assert requests[0][3]["context"] == "cov/total"
    assert "target_url" not in requests[0][3]


def test_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, requests = _run(tmp_path, monkeypatch, "--dry-run")
    assert (code, requests) == (0, [])
    assert capsys.readouterr().out == "coverage/project: 66.66% (3/4 lines, 1/2 branches)\n"


@pytest.mark.parametrize(("report", "message"), [(None, "No such file"), ("<coverage/>", "not a Cobertura")])
def test_bad_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    report: str | None,
    message: str,
) -> None:
    code, requests = _run(tmp_path, monkeypatch, report=report)
    assert (code, requests) == (1, [])
    assert message in capsys.readouterr().err


def test_bad_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.repoutils.coverage]\nstatus-name = 3\n")
    code, requests = _run(tmp_path, monkeypatch)
    assert (code, requests) == (1, [])
    assert "status-name must be a non-empty string" in capsys.readouterr().err


def test_bad_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        main(["main"])
    assert exit_info.value.code == 2
    assert "not a full commit SHA: 'main'" in capsys.readouterr().err


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

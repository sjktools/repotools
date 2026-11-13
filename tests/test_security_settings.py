import io
import subprocess
import time
import urllib.error
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import config, github
from repoutils.commands import security_settings
from repoutils.commands.security_settings import BY_KEY, SETTINGS

URL = f"{github.API_URL}/repos/o/r"
ALL_ON = {setting.key: True for setting in SETTINGS}
ALL_OFF = {setting.key: False for setting in SETTINGS}
ANALYSIS_FIELDS = {
    "dependabot-security-updates": "dependabot_security_updates",
    "secret-protection": "secret_scanning",
    "push-protection": "secret_scanning_push_protection",
}
TOGGLES = {
    "private-vulnerability-reporting": "private-vulnerability-reporting",
    "dependabot-alerts": "vulnerability-alerts",
    "dependabot-security-updates": "automated-security-fixes",
}


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", Message(), io.BytesIO(b'{"message": "no"}'))


class FakeGitHub:
    """Answers github.request like GitHub's API for o/r would, with state
    holding each setting by its key. A key in ignored isn't changed by a write,
    and refused maps a path, or a method and path such as "PUT x", to
    the HTTP status GitHub refuses it with."""

    def __init__(
        self,
        state: dict[str, bool],
        *,
        ignored: frozenset[str] = frozenset(),
        refused: dict[str, int] | None = None,
        admin: bool = True,
        codeql_delay: int = 0,
    ) -> None:
        self.state = dict(state)
        self.ignored = ignored
        self.refused = refused or {}
        self.admin = admin
        # How many reads of CodeQL default setup still say it's off after it's turned on.
        self.codeql_delay = codeql_delay
        self.pending_codeql = False
        self.calls: list[tuple[str, str, Any]] = []

    def _set(self, key: str, on: bool) -> None:
        if key not in self.ignored:
            self.state[key] = on

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        assert token == "tok"
        assert url.startswith(URL)
        path = url[len(URL) :].lstrip("/")
        self.calls.append((method, path, json_body))
        for refused in (path, f"{method} {path}"):
            if refused in self.refused:
                raise _http_error(url, self.refused[refused])
        if path == "":
            return self._repo(method, json_body)
        for key, toggle in TOGGLES.items():
            if path == toggle and method in ("PUT", "DELETE"):
                self._set(key, method == "PUT")
                return None
        if path == "vulnerability-alerts":
            if not self.state["dependabot-alerts"]:
                raise _http_error(url, 404)
            return None
        if path == "private-vulnerability-reporting":
            return {"enabled": self.state["private-vulnerability-reporting"]}
        if path == "code-scanning/default-setup":
            return self._codeql(method, json_body)
        raise AssertionError(f"unexpected {method} {path}")

    def _repo(self, method: str, body: Any) -> Any:
        if method == "PATCH":
            for key, field_name in ANALYSIS_FIELDS.items():
                if field_name in body["security_and_analysis"]:
                    self._set(key, body["security_and_analysis"][field_name]["status"] == "enabled")
        repo: dict[str, Any] = {"name": "r"}
        if self.admin:
            repo["security_and_analysis"] = {
                field_name: {"status": "enabled" if self.state[key] else "disabled"}
                for key, field_name in ANALYSIS_FIELDS.items()
            } | {"secret_scanning_validity_checks": {"status": "disabled"}}
        return repo

    def _codeql(self, method: str, body: Any) -> Any:
        if method == "PATCH":
            on = body == {"state": "configured"}
            assert on or body == {"state": "not-configured"}
            if on and self.codeql_delay:
                self.pending_codeql = True
            else:
                self._set("codeql-default-setup", on)
            return {"run_id": 1, "run_url": "https://run"} if on else {}
        if self.pending_codeql:
            self.codeql_delay -= 1
            if self.codeql_delay == 0:
                self.pending_codeql = False
                self._set("codeql-default-setup", True)
        state = "configured" if self.state["codeql-default-setup"] else "not-configured"
        return {"state": state, "languages": ["python"]}

    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]


CONFIG = """
[tool.repoutils.security-settings]
dependabot-alerts = true
secret-protection = true
push-protection = true
"""
MATCHING = ALL_OFF | {"dependabot-alerts": True, "secret-protection": True, "push-protection": True}


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gh: Callable[..., Any],
    argv: list[str],
    pyproject: str = CONFIG,
) -> int:
    (tmp_path / "pyproject.toml").write_text(pyproject)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    return security_settings.main([*argv, "--repo", "o/r"])


@pytest.mark.parametrize("mode", ["check", "ensure"])
def test_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    gh = FakeGitHub(MATCHING)
    assert _run(tmp_path, monkeypatch, gh, [mode]) == 0
    assert gh.writes() == []
    # Only the endpoints the table needs are read.
    assert [path for _, path, _ in gh.calls] == ["", "vulnerability-alerts"]
    expected = {
        "check": "All 3 setting(s) in o/r match pyproject.toml.\n",
        "ensure": "Updated 0 of 3 setting(s) in o/r.\n",
    }
    assert capsys.readouterr().out == expected[mode]


def test_check_reports_differences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(ALL_OFF)
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert gh.writes() == []
    assert capsys.readouterr().out == (
        "dependabot-alerts is false on GitHub, not true\n"
        "secret-protection is false on GitHub, not true\n"
        "push-protection is false on GitHub, not true\n"
        "3 of 3 setting(s) in o/r need `repoutils security-settings ensure`.\n"
    )


def test_check_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(ALL_OFF), ["check", "--exit-zero"]) == 0
    assert "3 of 3 setting(s) in o/r need" in capsys.readouterr().out


@pytest.mark.parametrize("refused", ["", "vulnerability-alerts"])
def test_check_exit_zero_still_fails_on_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], refused: str
) -> None:
    gh = FakeGitHub(ALL_OFF, refused={refused: 500})
    assert _run(tmp_path, monkeypatch, gh, ["check", "--exit-zero"]) == 1
    assert "HTTP 500 from" in capsys.readouterr().err


def test_check_exit_zero_still_fails_on_bad_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = "[tool.repoutils.security-settings]\nsecret-protection = 'yes'\n"
    assert _run(tmp_path, monkeypatch, FakeGitHub(ALL_OFF), ["check", "--exit-zero"], pyproject) == 1
    assert "secret-protection must be true or false" in capsys.readouterr().err


def test_a_token_that_cant_administer_the_repository_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Without admin access, vulnerability-alerts would 404, which reads as off:
    # check must fail rather than pass a table that says it's off.
    pyproject = "[tool.repoutils.security-settings]\ndependabot-alerts = false\n"
    gh = FakeGitHub(ALL_OFF, admin=False)
    assert _run(tmp_path, monkeypatch, gh, ["check", "--exit-zero"], pyproject) == 1
    assert "the token needs to be able to administer the repository" in capsys.readouterr().err
    assert [path for _, path, _ in gh.calls] == [""]


def test_ensure_turns_settings_on_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = "[tool.repoutils.security-settings]\n" + "".join(f"{key} = true\n" for key in reversed(list(ALL_ON)))
    gh = FakeGitHub(ALL_OFF)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], pyproject) == 0
    assert gh.state == ALL_ON
    assert gh.writes() == [
        ("PUT", "private-vulnerability-reporting", None),
        ("PUT", "vulnerability-alerts", None),
        ("PUT", "automated-security-fixes", None),
        ("PATCH", "code-scanning/default-setup", {"state": "configured"}),
        ("PATCH", "", {"security_and_analysis": {"secret_scanning": {"status": "enabled"}}}),
        ("PATCH", "", {"security_and_analysis": {"secret_scanning_push_protection": {"status": "enabled"}}}),
    ]
    out = capsys.readouterr().out
    assert "GitHub started CodeQL default setup: https://run\n" in out
    assert "updated: dependabot-alerts is false on GitHub, not true\n" in out
    assert out.endswith("Updated 6 of 6 setting(s) in o/r.\n")


def test_ensure_turns_settings_off_in_reverse_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pyproject = "[tool.repoutils.security-settings]\n" + "".join(f"{key} = false\n" for key in ALL_OFF)
    gh = FakeGitHub(ALL_ON)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], pyproject) == 0
    assert gh.state == ALL_OFF
    assert gh.writes() == [
        ("PATCH", "", {"security_and_analysis": {"secret_scanning_push_protection": {"status": "disabled"}}}),
        ("PATCH", "", {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}),
        ("PATCH", "code-scanning/default-setup", {"state": "not-configured"}),
        ("DELETE", "automated-security-fixes", None),
        ("DELETE", "vulnerability-alerts", None),
        ("DELETE", "private-vulnerability-reporting", None),
    ]


def test_ensure_waits_for_codeql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = "[tool.repoutils.security-settings]\ncodeql-default-setup = true\n"
    gh = FakeGitHub(ALL_OFF, codeql_delay=3)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], pyproject) == 0
    assert gh.state["codeql-default-setup"]
    assert capsys.readouterr().out.endswith("Updated 1 of 1 setting(s) in o/r.\n")


def test_ensure_gives_up_waiting_for_codeql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = "[tool.repoutils.security-settings]\ncodeql-default-setup = true\n"
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    gh = FakeGitHub(ALL_OFF, codeql_delay=1_000)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], pyproject) == 1
    err = capsys.readouterr().err
    assert "GitHub didn't apply the change: codeql-default-setup is false on GitHub, not true\n" in err


def test_ensure_fails_if_github_doesnt_apply_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(ALL_OFF, ignored=frozenset({"push-protection"}))
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 1
    err = capsys.readouterr().err
    assert "GitHub didn't apply the change: push-protection is false on GitHub, not true\n" in err
    assert "organization's security configuration" in err


def test_ensure_reports_a_refused_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(ALL_OFF)
    gh.refused = {"PUT vulnerability-alerts": 422}
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 1
    assert gh.writes() == [("PUT", "vulnerability-alerts", None)]
    assert "HTTP 422 from" in capsys.readouterr().err


def test_dump_round_trips_through_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = ALL_OFF | {"dependabot-alerts": True, "codeql-default-setup": True}
    assert _run(tmp_path, monkeypatch, FakeGitHub(state), ["dump"], "") == 0
    dumped = capsys.readouterr()
    assert dumped.err == ""
    assert dumped.out == (
        "[tool.repoutils.security-settings]\n"
        "# Private vulnerability reporting\n"
        "private-vulnerability-reporting = false\n"
        "# Dependabot alerts\n"
        "dependabot-alerts = true\n"
        "# Dependabot security updates\n"
        "dependabot-security-updates = false\n"
        "# CodeQL analysis (default setup)\n"
        "codeql-default-setup = true\n"
        "# Secret Protection\n"
        "secret-protection = false\n"
        "# Push protection\n"
        "push-protection = false\n"
    )
    gh = FakeGitHub(state)
    assert _run(tmp_path, monkeypatch, gh, ["check"], dumped.out) == 0
    assert "All 6 setting(s) in o/r match" in capsys.readouterr().out


def test_dump_leaves_out_a_setting_github_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(ALL_OFF, refused={"code-scanning/default-setup": 403})
    assert _run(tmp_path, monkeypatch, gh, ["dump"], "") == 0
    out, err = capsys.readouterr()
    assert "codeql-default-setup" not in out
    assert "push-protection = false\n" in out
    assert err == "Left out codeql-default-setup: GitHub refused to read it (HTTP 403)\n"


def test_no_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(ALL_OFF)
    assert _run(tmp_path, monkeypatch, gh, ["check"], "") == 1
    assert gh.calls == []
    assert "No security settings are defined" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ("dependency-graph = true", "can't set dependency-graph: GitHub's API can't turn the dependency graph"),
        ("copilot-autofix = true", "can't set copilot-autofix"),
        ("secret-scanning = true", "unknown setting: secret-scanning"),
        ("push-protection = 1", "push-protection must be true or false"),
        (
            "dependabot-alerts = false\ndependabot-security-updates = true",
            "dependabot-security-updates can't be true while dependabot-alerts is false",
        ),
        (
            "secret-protection = false\npush-protection = true",
            "push-protection can't be true while secret-protection is false",
        ),
    ],
)
def test_load_settings_rejects(tmp_path: Path, table: str, message: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f"[tool.repoutils.security-settings]\n{table}\n")
    with pytest.raises(config.ConfigError, match=message):
        security_settings.load_settings(tmp_path)


@pytest.mark.parametrize(
    ("path", "response", "message"),
    [
        ("", {"security_and_analysis": {"dependabot_security_updates": {"status": "paused"}}}, "'paused' as"),
        ("private-vulnerability-reporting", {"enabled": None}, "None for private vulnerability reporting"),
        ("code-scanning/default-setup", {"state": "odd"}, "'odd' as CodeQL default setup's state"),
    ],
)
def test_unexpected_responses_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
    response: dict[str, Any],
    message: str,
) -> None:
    gh = FakeGitHub(ALL_OFF)

    def request(method: str, url: str, token: str, **kwargs: Any) -> Any:
        if url.rstrip("/") == f"{URL}/{path}".rstrip("/"):
            return response
        return gh(method, url, token, **kwargs)

    assert _run(tmp_path, monkeypatch, request, ["dump"], "") == 1
    assert message in capsys.readouterr().err


def test_gh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def gh_output(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "gh_output", gh_output)
    assert security_settings.main(["check"]) == 1
    assert capsys.readouterr().err == "gh auth token failed:\nnot logged in\n"


def test_setting_keys_are_unique() -> None:
    assert len(BY_KEY) == len(SETTINGS)

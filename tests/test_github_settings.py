import os
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from repoutils import github
from repoutils.commands import github_settings
from tests import (
    test_actions_settings,
    test_deploy_keys,
    test_environments,
    test_labels,
    test_repo_settings,
    test_rulesets,
    test_secrets,
    test_security_settings,
)

ALL = [command.name for command in github_settings.COMMANDS]

PYPROJECT = """\
[[tool.repoutils.labels]]
name = "bug"

[tool.repoutils.repo-settings]
[tool.repoutils.rulesets.main]
[tool.repoutils.environments.release]
[tool.repoutils.actions-settings]
[tool.repoutils.deploy-keys]
[tool.repoutils.security-settings]

[tool.repoutils.secrets]
actions = ["TOKEN"]
"""

Calls = list[tuple[str, list[str], str | None]]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Calls:
    """Replaces every command with one that records its arguments and GITHUB_TOKEN, and fails if its name is in FAIL."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    recorded: Calls = []

    def fake(name: str) -> Callable[[list[str]], int]:
        def main(args: list[str]) -> int:
            recorded.append((name, args, os.environ.get("GITHUB_TOKEN")))
            return 1 if name in os.environ.get("FAIL", "").split(",") else 0

        return main

    monkeypatch.setattr(
        github_settings,
        "COMMANDS",
        [
            github_settings.Command(c.name, fake(c.name), c.configured, c.has_ensure, c.has_fail_on_unexpected)
            for c in github_settings.COMMANDS
        ],
    )
    return recorded


def _write(text: str = PYPROJECT) -> None:
    Path("pyproject.toml").write_text(text)


def test_check_runs_every_configured_command(calls: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    _write()
    assert github_settings.main(["check", "--repo", "o/r", "--exit-zero"]) == 0
    assert calls == [(name, ["check", "--repo", "o/r", "--exit-zero"], "tok") for name in ALL]
    assert "== repoutils labels check" in capsys.readouterr().out


def test_check_forwards_fail_on_unexpected_where_supported(calls: Calls) -> None:
    _write()
    assert github_settings.main(["check", "--repo", "o/r", "--fail-on-unexpected"]) == 0
    with_option = [name for name, args, _ in calls if "--fail-on-unexpected" in args]
    assert with_option == ["labels", "rulesets", "environments", "secrets", "deploy-keys"]


def test_check_skips_unconfigured_commands(calls: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    # Secrets that only check-workflows reads don't count.
    _write('[tool.repoutils.repo-settings]\n[tool.repoutils.secrets]\nother = ["ORG"]\n')
    assert github_settings.main(["check", "--repo", "o/r"]) == 0
    assert [name for name, _, _ in calls] == ["repo-settings"]
    err = capsys.readouterr().err
    assert "Skipped labels: pyproject.toml doesn't configure it." in err
    assert "Skipped secrets: pyproject.toml doesn't configure it." in err


def test_empty_deploy_keys_table_counts_as_configured(calls: Calls) -> None:
    _write("[tool.repoutils.deploy-keys]\n[tool.repoutils.secrets]\nenvironments = {}\n")
    assert github_settings.main(["check", "--repo", "o/r"]) == 0
    assert [name for name, _, _ in calls] == ["secrets", "deploy-keys"]


def test_fails_if_nothing_is_configured(calls: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    assert github_settings.main(["check", "--repo", "o/r"]) == 1
    assert calls == []
    assert "configures none of the GitHub settings commands" in capsys.readouterr().err


@pytest.mark.parametrize(
    "secrets_table",
    [
        'secrets = ["TOKEN"]',
        '[tool.repoutils.secrets]\nbogus = ["TOKEN"]',
        '[tool.repoutils.secrets]\nactions = "TOKEN"',
    ],
)
def test_bad_secrets_table_fails_rather_than_being_skipped(
    calls: Calls, capsys: pytest.CaptureFixture[str], secrets_table: str
) -> None:
    _write(f"[tool.repoutils]\n{secrets_table}\n" if secrets_table.startswith("secrets") else secrets_table + "\n")
    assert github_settings.main(["check", "--repo", "o/r", "--exit-zero"]) == 1
    assert calls == []
    assert "secrets" in capsys.readouterr().err


def test_bad_pyproject_fails(calls: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    _write("[tool.repoutils\n")
    assert github_settings.main(["check", "--repo", "o/r"]) == 1
    assert calls == []
    assert "pyproject.toml" in capsys.readouterr().err


def test_check_runs_every_command_and_fails_if_any_does(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write()
    monkeypatch.setenv("FAIL", "rulesets,secrets")
    assert github_settings.main(["check", "--repo", "o/r"]) == 1
    assert [name for name, _, _ in calls] == ALL
    assert "Failed: rulesets, secrets." in capsys.readouterr().err


def test_exit_zero_still_fails_on_an_error(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    # A command given --exit-zero only fails if it couldn't check, so that must still fail the whole run.
    _write()
    monkeypatch.setenv("FAIL", "environments")
    assert github_settings.main(["check", "--repo", "o/r", "--exit-zero"]) == 1


def test_ensure_skips_secrets(calls: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    _write()
    assert github_settings.main(["ensure", "--repo", "o/r"]) == 0
    assert [name for name, _, _ in calls] == [name for name in ALL if name != "secrets"]
    assert all(args == ["ensure", "--repo", "o/r"] for _, args, _ in calls)
    assert "Skipped secrets: it has no ensure." in capsys.readouterr().err


def test_ensure_stops_at_the_first_failure(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write()
    monkeypatch.setenv("FAIL", "environments")
    assert github_settings.main(["ensure", "--repo", "o/r"]) == 1
    assert [name for name, _, _ in calls] == ["labels", "repo-settings", "rulesets", "environments"]
    err = capsys.readouterr().err
    assert "without running actions-settings, deploy-keys, security-settings." in err


def test_ensure_failing_last_stops_quietly(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write()
    monkeypatch.setenv("FAIL", "security-settings")
    assert github_settings.main(["ensure", "--repo", "o/r"]) == 1
    assert "Stopped" not in capsys.readouterr().err


def test_dump_runs_everything_without_pyproject(calls: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    _write("not toml [")
    assert github_settings.main(["dump", "--repo", "o/r"]) == 0
    assert calls == [(name, ["dump", "--repo", "o/r"], "tok") for name in ALL]
    # Only blank lines between the dumps, so the output stays TOML.
    assert capsys.readouterr().out == "\n" * (len(ALL) - 1)


def test_looks_up_token_and_repo_once(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    lookups: list[tuple[str, ...]] = []

    def gh_output(*args: str) -> str:
        lookups.append(args)
        return "gh-token" if args[0] == "auth" else "gh/repo"

    monkeypatch.setattr(github, "gh_output", gh_output)
    assert github_settings.main(["dump"]) == 0
    assert len(lookups) == 2
    assert calls[0] == ("labels", ["dump", "--repo", "gh/repo"], "gh-token")
    assert "GITHUB_TOKEN" not in os.environ


def test_restores_existing_token(calls: Calls) -> None:
    os.environ["GITHUB_TOKEN"] = "mine"
    assert github_settings.main(["dump", "--repo", "o/r"]) == 0
    assert os.environ["GITHUB_TOKEN"] == "mine"


def test_gh_failure_fails(calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")

    def gh_output(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    monkeypatch.setattr(github, "gh_output", gh_output)
    assert github_settings.main(["dump", "--repo", "o/r"]) == 1
    assert calls == []
    assert "not logged in" in capsys.readouterr().err


def test_real_dumps_join_into_valid_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    # Each command's own test fake, swapped in while that command runs, as some APIs (the repository, its
    # environments) are read by more than one command.
    fakes: dict[str, Callable[..., object]] = {
        "labels": test_labels.FakeGitHub(test_labels.TRICKY),
        "repo-settings": test_repo_settings.FakeGitHub(test_repo_settings.EVERYTHING),
        "rulesets": test_rulesets.FakeGitHub(test_rulesets.MATCHING, test_rulesets.TAG_RULESET),
        "environments": test_environments.FakeGitHub([test_environments._env("pypi", wait=10)]),
        "actions-settings": test_actions_settings.FakeGitHub(test_actions_settings.EVERYTHING),
        "secrets": test_secrets.FakeGitHub(["ALPHA"], [], {"pypi": ["TOKEN"]}),
        "deploy-keys": test_deploy_keys.FakeGitHub([test_deploy_keys._key(1, "release", write=True)]),
        "security-settings": test_security_settings.FakeGitHub(test_security_settings.MATCHING),
    }

    def with_fake(command: github_settings.Command) -> Callable[[list[str]], int]:
        def run(args: list[str]) -> int:
            monkeypatch.setattr(github, "request", fakes[command.name])
            return command.run(args)

        return run

    monkeypatch.setattr(
        github_settings,
        "COMMANDS",
        [
            github_settings.Command(c.name, with_fake(c), c.configured, c.has_ensure, c.has_fail_on_unexpected)
            for c in github_settings.COMMANDS
        ],
    )
    assert github_settings.main(["dump", "--repo", "o/r"]) == 0
    dumped = tomllib.loads(capsys.readouterr().out)["tool"]["repoutils"]
    assert list(dumped) == ALL

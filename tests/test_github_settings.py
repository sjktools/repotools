from collections.abc import Callable

import pytest

from repoutils.commands import github_settings


def test_check_runs_every_command_with_repo_and_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_main(name: str) -> Callable[[list[str]], int]:
        def main(args: list[str]) -> int:
            calls.append((name, args))
            return 0

        return main

    monkeypatch.setattr(
        github_settings,
        "COMMANDS",
        [
            github_settings.Command(command.name, fake_main(command.name), command.has_ensure)
            for command in github_settings.COMMANDS
        ],
    )
    assert github_settings.main(["check", "--repo", "o/r", "--exit-zero"]) == 0
    assert calls == [
        ("labels", ["check", "--repo", "o/r", "--exit-zero"]),
        ("repo-settings", ["check", "--repo", "o/r", "--exit-zero"]),
        ("rulesets", ["check", "--repo", "o/r", "--exit-zero"]),
        ("environments", ["check", "--repo", "o/r", "--exit-zero"]),
        ("actions-settings", ["check", "--repo", "o/r", "--exit-zero"]),
        ("secrets", ["check", "--repo", "o/r", "--exit-zero"]),
        ("deploy-keys", ["check", "--repo", "o/r", "--exit-zero"]),
        ("security-settings", ["check", "--repo", "o/r", "--exit-zero"]),
    ]


def test_ensure_skips_commands_without_ensure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    def fake_main(name: str) -> Callable[[list[str]], int]:
        def main(args: list[str]) -> int:
            assert args == ["ensure"]
            calls.append(name)
            return 0

        return main

    monkeypatch.setattr(
        github_settings,
        "COMMANDS",
        [
            github_settings.Command(command.name, fake_main(command.name), command.has_ensure)
            for command in github_settings.COMMANDS
        ],
    )
    assert github_settings.main(["ensure"]) == 0
    assert calls == [
        "labels",
        "repo-settings",
        "rulesets",
        "environments",
        "actions-settings",
        "deploy-keys",
        "security-settings",
    ]


def test_returns_non_zero_if_any_command_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_main(name: str) -> Callable[[list[str]], int]:
        def main(args: list[str]) -> int:
            return 1 if name == "rulesets" else 0

        return main

    monkeypatch.setattr(
        github_settings,
        "COMMANDS",
        [
            github_settings.Command(command.name, fake_main(command.name), command.has_ensure)
            for command in github_settings.COMMANDS
        ],
    )
    assert github_settings.main(["dump"]) == 1

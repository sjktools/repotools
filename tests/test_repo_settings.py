import io
import re
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import github
from repoutils.commands import repo_settings
from repoutils.commands.repo_settings import BY_KEY

CONFIG = """
[tool.repoutils.repo-settings]
description = "Tools"
issues = true
wiki = false
pull-request-creation = "collaborators"
allow-auto-merge = true

[tool.repoutils.repo-settings.squash-merge]
allow = true
message = "commit-messages"
"""

# What GitHub returns for a repository that matches CONFIG, plus unrelated fields.
MATCHING: dict[str, Any] = {
    "name": "r",
    "description": "Tools",
    "homepage": None,
    "has_issues": True,
    "has_wiki": False,
    "pull_request_creation_policy": "collaborators_only",
    "allow_auto_merge": True,
    "allow_squash_merge": True,
    "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
    "squash_merge_commit_message": "COMMIT_MESSAGES",
}
URL = f"{github.API_URL}/repos/o/r"


class FakeGitHub:
    """Answers github.request like the repository API of o/r would, applying
    each PATCH field except those in ignored."""

    def __init__(self, existing: dict[str, Any], ignored: frozenset[str] = frozenset()) -> None:
        self.existing = dict(existing)
        self.ignored = ignored
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        assert token == "tok"
        assert url == URL
        self.calls.append((method, url, json_body))
        if method == "PATCH":
            self.existing.update({k: v for k, v in json_body.items() if k not in self.ignored})
        return dict(self.existing)

    def writes(self) -> list[Any]:
        return [body for method, _, body in self.calls if method != "GET"]


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gh: FakeGitHub,
    argv: list[str],
    config: str = CONFIG,
) -> int:
    (tmp_path / "pyproject.toml").write_text(config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    return repo_settings.main([*argv, "--repo", "o/r"])


@pytest.mark.parametrize("mode", ["check", "ensure"])
def test_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    gh = FakeGitHub(MATCHING)
    assert _run(tmp_path, monkeypatch, gh, [mode]) == 0
    assert gh.writes() == []
    expected = {
        "check": "All 7 setting(s) in o/r match pyproject.toml.\n",
        "ensure": "Updated 0 of 7 setting(s) in o/r.\n",
    }
    assert capsys.readouterr().out == expected[mode]


DIFFERENT = MATCHING | {
    "description": None,
    "has_wiki": True,
    "pull_request_creation_policy": "all",
    "squash_merge_commit_message": "BLANK",
}


def test_check_reports_differences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(DIFFERENT)
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert gh.writes() == []
    assert capsys.readouterr().out == (
        "description is '' on GitHub, not 'Tools'\n"
        "wiki is True on GitHub, not False\n"
        "pull-request-creation is 'all' on GitHub, not 'collaborators'\n"
        "squash-merge.message is 'blank' on GitHub, not 'commit-messages'\n"
        "4 of 7 setting(s) in o/r need `repoutils repo-settings ensure`.\n"
    )


def test_check_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(DIFFERENT), ["check", "--exit-zero"]) == 0
    assert "4 of 7 setting(s) in o/r need" in capsys.readouterr().out


def test_ensure_changes_differences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(DIFFERENT)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 0
    assert gh.writes() == [
        {
            "description": "Tools",
            "has_wiki": False,
            "pull_request_creation_policy": "collaborators_only",
            "squash_merge_commit_message": "COMMIT_MESSAGES",
            # GitHub needs the title with the message, so it's sent unchanged.
            "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
        }
    ]
    assert gh.existing == MATCHING
    out = capsys.readouterr().out
    assert out.startswith("updated: description is '' on GitHub, not 'Tools'\n")
    assert out.endswith("Updated 4 of 7 setting(s) in o/r.\n")


def test_ensure_fails_if_github_ignores_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(DIFFERENT, ignored=frozenset({"has_wiki"}))
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 1
    assert capsys.readouterr().err == "GitHub didn't apply the change: wiki is True on GitHub, not False\n"


def test_missing_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub({k: v for k, v in MATCHING.items() if "squash" not in k})
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 1
    assert gh.writes() == []
    assert capsys.readouterr().err == (
        "GitHub didn't return allow_squash_merge, squash_merge_commit_message, squash_merge_commit_title: "
        "the token may not have access to the repository's settings\n"
    )


def test_missing_title_for_a_configured_message() -> None:
    settings = {BY_KEY["squash-merge.message"]: "blank"}
    with pytest.raises(RuntimeError, match="didn't return squash_merge_commit_title"):
        repo_settings.plan(settings, {"squash_merge_commit_message": "PR_BODY"})


def test_ensure_patch_response_missing_a_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(DIFFERENT)
    real = gh.__call__

    def request(method: str, url: str, token: str, **kwargs: Any) -> Any:
        response = real(method, url, token, **kwargs)
        if method == "PATCH":
            del response["has_wiki"]
        return response

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", request)
    assert repo_settings.main(["ensure", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err.startswith("Updated o/r, but couldn't check GitHub applied every change: ")


def test_title_alone_is_sent_alone() -> None:
    settings = {BY_KEY["merge-commit.title"]: "pr-title"}
    existing = {"merge_commit_title": "MERGE_MESSAGE", "merge_commit_message": "PR_TITLE"}
    assert repo_settings.plan(settings, existing).fields == {"merge_commit_title": "PR_TITLE"}


def test_unknown_api_value_is_shown_as_is() -> None:
    settings = {BY_KEY["merge-commit.message"]: "blank"}
    existing = {"merge_commit_title": "PR_TITLE", "merge_commit_message": "SOMETHING_NEW"}
    assert repo_settings.plan(settings, existing).changes == [
        "merge-commit.message is 'SOMETHING_NEW' on GitHub, not 'blank'"
    ]


def test_no_settings_defined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({})
    assert _run(tmp_path, monkeypatch, gh, ["check"], "[project]\nname = 'x'\n") == 1
    assert gh.calls == []
    assert "No repository settings are defined" in capsys.readouterr().err


def test_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def request(*args: object, **kwargs: object) -> Any:
        raise urllib.error.HTTPError("https://x/repos/o/r", 403, "Forbidden", Message(), io.BytesIO(b"no access"))

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", request)
    # --exit-zero ignores settings needing ensure, but not an error.
    assert repo_settings.main(["check", "--exit-zero", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == "HTTP 403 from https://x/repos/o/r:\nno access\n"


def test_gh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def gh_output(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "gh_output", gh_output)
    assert repo_settings.main(["check"]) == 1
    assert capsys.readouterr().err == "gh auth token failed:\nnot logged in\n"


TABLE = "[tool.repoutils.repo-settings]\n"


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ("[tool.repoutils]\nrepo-settings = 1\n", "must be a table"),
        (TABLE + "wikis = true\n", "unknown setting: wikis"),
        (TABLE + "squash-merge = { allowed = true }\n", "unknown setting: squash-merge.allowed"),
        (TABLE + "squash-merge = true\n", "unknown setting: squash-merge"),
        (TABLE + "name = 'x'\n", "can't set name: repoutils doesn't manage the repository's name"),
        (TABLE + "default-branch = 'x'\n", "can't set default-branch"),
        (TABLE + "visibility = 'public'\n", "can't set visibility"),
        (TABLE + "archived = false\n", "can't set archived"),
        (TABLE + "is-template = false\n", "can't set is-template"),
        (TABLE + "issues = 'yes'\n", "issues must be true or false"),
        (TABLE + "issues = 1\n", "issues must be true or false"),
        (TABLE + "description = false\n", "description must be a string"),
        (TABLE + "pull-request-creation = 'collaborators_only'\n", 'must be one of "all", "collaborators"'),
        (TABLE + "merge-commit.title = 'PR_TITLE'\n", 'merge-commit.title must be one of "pr-title"'),
        (TABLE + "pull-request-creation = ['all']\n", "pull-request-creation must be one of"),
    ],
)
def test_bad_config(tmp_path: Path, config: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config)
    with pytest.raises(ValueError, match=re.escape(error)):
        repo_settings.load_settings(tmp_path)


def test_load_settings(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    assert {setting.key: value for setting, value in repo_settings.load_settings(tmp_path).items()} == {
        "description": "Tools",
        "issues": True,
        "wiki": False,
        "pull-request-creation": "collaborators",
        "allow-auto-merge": True,
        "squash-merge.allow": True,
        "squash-merge.message": "commit-messages",
    }


def test_every_setting_has_a_distinct_key_and_field() -> None:
    settings = repo_settings.SETTINGS
    assert len({s.key for s in settings}) == len({s.api_field for s in settings}) == len(settings)


def test_bad_config_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub({})
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], TABLE + "name = 'x'\n") == 1
    assert gh.calls == []
    assert "can't set name" in capsys.readouterr().err


# Every setting, as GitHub returns it.
EVERYTHING: dict[str, Any] = {
    "name": "r",
    "description": 'Tools with "quotes"',
    "homepage": None,
    "has_issues": True,
    "has_projects": False,
    "has_wiki": False,
    "has_discussions": True,
    "has_downloads": True,
    "has_pull_requests": True,
    "pull_request_creation_policy": "collaborators_only",
    "allow_merge_commit": False,
    "merge_commit_title": "MERGE_MESSAGE",
    "merge_commit_message": "PR_TITLE",
    "allow_squash_merge": True,
    "squash_merge_commit_title": "PR_TITLE",
    "squash_merge_commit_message": "PR_BODY",
    "allow_rebase_merge": False,
    "allow_auto_merge": True,
    "allow_update_branch": True,
    "delete_branch_on_merge": True,
    "web_commit_signoff_required": False,
}


def test_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # dump doesn't need pyproject.toml to set anything.
    assert _run(tmp_path, monkeypatch, FakeGitHub(EVERYTHING), ["dump"], "") == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out == (
        "[tool.repoutils.repo-settings]\n"
        'description = "Tools with \\"quotes\\""\n'
        'homepage = ""\n'
        "issues = true\n"
        "projects = false\n"
        "wiki = false\n"
        "discussions = true\n"
        "downloads = true\n"
        "pull-requests = true\n"
        'pull-request-creation = "collaborators"\n'
        "allow-auto-merge = true\n"
        "allow-update-branch = true\n"
        "delete-branch-on-merge = true\n"
        "require-web-commit-signoff = false\n"
        "\n"
        "[tool.repoutils.repo-settings.merge-commit]\n"
        "allow = false\n"
        'title = "merge-message"\n'
        'message = "pr-title"\n'
        "\n"
        "[tool.repoutils.repo-settings.squash-merge]\n"
        "allow = true\n"
        'title = "pr-title"\n'
        'message = "pr-body"\n'
        "\n"
        "[tool.repoutils.repo-settings.rebase-merge]\n"
        "allow = false\n"
    )
    # Pasted into pyproject.toml, it gives every setting as GitHub has it.
    gh = FakeGitHub(EVERYTHING)
    assert _run(tmp_path, monkeypatch, gh, ["check"], out) == 0
    assert capsys.readouterr().out == f"All {len(BY_KEY)} setting(s) in o/r match pyproject.toml.\n"
    assert gh.writes() == []


def test_dump_leaves_out_what_it_cant_give(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # GitHub may leave out a field (some need access to the settings) or add an enum value.
    existing = {k: v for k, v in EVERYTHING.items() if "merge" not in k} | {"pull_request_creation_policy": "new"}
    assert _run(tmp_path, monkeypatch, FakeGitHub(existing), ["dump"], "") == 0
    out, err = capsys.readouterr()
    assert "merge" not in out and "pull-request-creation" not in out
    assert err.startswith(
        "Left out pull-request-creation: GitHub's value 'new' isn't one [tool.repoutils.repo-settings] can give\n"
        "Left out merge-commit.allow: GitHub didn't return allow_merge_commit\n"
    )
    assert _run(tmp_path, monkeypatch, FakeGitHub(existing), ["check"], out) == 0

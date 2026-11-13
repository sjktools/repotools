import io
import re
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import config, github
from repoutils.commands import actions_settings
from repoutils.commands.actions_settings import BY_KEY, OIDC, PERMISSIONS, SELECTED, WORKFLOW

CONFIG = """
[tool.repoutils.actions-settings]
allowed-actions = "selected"
default-token-permissions = "read"
fork-pr-approval = "all-external-contributors"
retention-days = 30

[tool.repoutils.actions-settings.selected-actions]
github-owned = true
patterns = ["astral-sh/*", "extractions/setup-just@*"]

[tool.repoutils.actions-settings.oidc]
immutable-subject = true
"""
BASE = f"{github.API_URL}/repos/o/r/"

# What GitHub returns for a repository that matches CONFIG, plus unrelated fields.
MATCHING: dict[str, dict[str, Any]] = {
    "actions/permissions": {
        "enabled": True,
        "allowed_actions": "selected",
        "selected_actions_url": "https://x",
        "sha_pinning_required": False,
    },
    "actions/permissions/selected-actions": {
        "github_owned_allowed": True,
        "verified_allowed": False,
        "patterns_allowed": ["extractions/setup-just@*", "astral-sh/*"],
    },
    "actions/permissions/workflow": {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
    "actions/permissions/fork-pr-contributor-approval": {"approval_policy": "all_external_contributors"},
    "actions/permissions/artifact-and-log-retention": {"days": 30, "maximum_allowed_days": 90},
    "actions/cache/retention-limit": {"max_cache_retention_days": 7},
    "actions/cache/storage-limit": {"max_cache_size_gb": 10},
    "actions/oidc/customization/sub": {"use_default": True, "use_immutable_subject": True, "sub_claim_prefix": "x"},
}


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "Error", Message(), io.BytesIO(b"nope"))


class FakeGitHub:
    """Answers github.request like the Actions settings API of o/r would,
    applying each PUT field except those in ignored."""

    def __init__(self, existing: dict[str, dict[str, Any]], ignored: frozenset[str] = frozenset()) -> None:
        self.existing = {path: dict(response) for path, response in existing.items()}
        self.ignored = ignored
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        assert token == "tok"
        assert url.startswith(BASE)
        path = url.removeprefix(BASE)
        self.calls.append((method, path, json_body))
        if path not in self.existing:
            raise _http_error(404)
        permissions = self.existing["actions/permissions"]
        if path == SELECTED.path and permissions.get("allowed_actions") != "selected":
            raise _http_error(409)
        if method == "PUT":
            self.existing[path].update({k: v for k, v in json_body.items() if k not in self.ignored})
            return None
        return dict(self.existing[path])

    def writes(self) -> list[tuple[str, Any]]:
        return [(path, body) for method, path, body in self.calls if method != "GET"]


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gh: Any,
    argv: list[str],
    config_text: str = CONFIG,
) -> int:
    (tmp_path / "pyproject.toml").write_text(config_text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    return actions_settings.main([*argv, "--repo", "o/r"])


@pytest.mark.parametrize("mode", ["check", "ensure"])
def test_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    gh = FakeGitHub(MATCHING)
    assert _run(tmp_path, monkeypatch, gh, [mode]) == 0
    assert gh.writes() == []
    # Only the endpoints pyproject.toml needs are read.
    assert {path for _, path, _ in gh.calls} == set(MATCHING) - {
        "actions/cache/retention-limit",
        "actions/cache/storage-limit",
    }
    expected = {
        "check": "All 7 setting(s) in o/r match pyproject.toml.\n",
        "ensure": "Updated 0 of 7 setting(s) in o/r.\n",
    }
    assert capsys.readouterr().out == expected[mode]


DIFFERENT = MATCHING | {
    "actions/permissions": {"enabled": True, "allowed_actions": "all", "sha_pinning_required": True},
    "actions/permissions/selected-actions": {
        "github_owned_allowed": False,
        "verified_allowed": True,
        "patterns_allowed": [],
    },
    "actions/permissions/workflow": {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True},
    "actions/permissions/artifact-and-log-retention": {"days": 90, "maximum_allowed_days": 90},
    "actions/oidc/customization/sub": {
        "use_default": False,
        "include_claim_keys": ["repo"],
        "use_immutable_subject": False,
    },
}


def test_check_reports_differences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(DIFFERENT)
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert gh.writes() == []
    # GitHub refuses selected-actions while allowed-actions isn't "selected", so check doesn't ask.
    assert ("GET", SELECTED.path, None) not in gh.calls
    assert capsys.readouterr().out == (
        "allowed-actions is 'all' on GitHub, not 'selected'\n"
        "selected-actions.github-owned isn't in effect on GitHub, as allowed-actions isn't \"selected\"\n"
        "selected-actions.patterns isn't in effect on GitHub, as allowed-actions isn't \"selected\"\n"
        "default-token-permissions is 'write' on GitHub, not 'read'\n"
        "retention-days is 90 on GitHub, not 30\n"
        "oidc.immutable-subject is False on GitHub, not True\n"
        "6 of 7 setting(s) in o/r need `repoutils actions-settings ensure`.\n"
    )


def test_check_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(DIFFERENT), ["check", "--exit-zero"]) == 0
    assert "6 of 7 setting(s) in o/r need" in capsys.readouterr().out


def test_ensure_changes_differences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # With the default template, the claim keys are left out of the PUT.
    oidc = {"use_default": True, "include_claim_keys": [], "use_immutable_subject": False}
    gh = FakeGitHub(DIFFERENT | {OIDC.path: oidc})
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 0
    assert gh.writes() == [
        # Each PUT sends what the table leaves out as GitHub has it.
        ("actions/permissions", {"enabled": True, "allowed_actions": "selected", "sha_pinning_required": True}),
        (
            "actions/permissions/selected-actions",
            {
                "github_owned_allowed": True,
                "verified_allowed": True,
                "patterns_allowed": ["astral-sh/*", "extractions/setup-just@*"],
            },
        ),
        (
            "actions/permissions/workflow",
            {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True},
        ),
        ("actions/permissions/artifact-and-log-retention", {"days": 30}),
        ("actions/oidc/customization/sub", {"use_default": True, "use_immutable_subject": True}),
    ]
    out = capsys.readouterr().out
    assert out.startswith("updated: allowed-actions is 'all' on GitHub, not 'selected'\n")
    # The selected actions GitHub had once allowed-actions was "selected" are what it compares.
    assert "updated: selected-actions.github-owned is False on GitHub, not True\n" in out
    assert out.endswith("Updated 6 of 7 setting(s) in o/r.\n")


def test_oidc_keys_are_sent_with_a_custom_template() -> None:
    settings = {BY_KEY["oidc.immutable-subject"]: True}
    response = {"use_default": False, "include_claim_keys": ["repo"], "use_immutable_subject": False}
    body, _ = actions_settings.diff(settings, response)
    assert body == {"use_default": False, "include_claim_keys": ["repo"], "use_immutable_subject": True}


def test_disabling_actions() -> None:
    settings = {BY_KEY["allowed-actions"]: "disabled"}
    body, changes = actions_settings.diff(settings, {"enabled": True, "allowed_actions": "local_only"})
    assert body == {"enabled": False, "allowed_actions": "local_only"}
    assert changes == ["allowed-actions is 'owner' on GitHub, not 'disabled'"]
    assert actions_settings.diff(settings, {"enabled": False}) == ({}, [])
    # Enabling them again sends the policy too.
    body, _ = actions_settings.diff({BY_KEY["allowed-actions"]: "all"}, {"enabled": False})
    assert body == {"enabled": True, "allowed_actions": "all"}


def test_unknown_api_value_is_shown_as_is() -> None:
    settings = {BY_KEY["fork-pr-approval"]: "first-time-contributors"}
    assert actions_settings.diff(settings, {"approval_policy": "something_new"})[1] == [
        "fork-pr-approval is 'something_new' on GitHub, not 'first-time-contributors'"
    ]


def test_ensure_fails_if_github_ignores_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(DIFFERENT, ignored=frozenset({"days"}))
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 1
    assert capsys.readouterr().err == "GitHub didn't apply the change: retention-days is 90 on GitHub, not 30\n"


def test_missing_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(MATCHING | {"actions/permissions/workflow": {}})
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 1
    assert gh.writes() == []
    assert capsys.readouterr().err == (
        "GitHub didn't return default_workflow_permissions from actions/permissions/workflow: "
        "the token may not have access to the repository's Actions settings\n"
    )


def test_ensure_cant_check_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(DIFFERENT)
    real = gh.__call__

    def request(method: str, url: str, token: str, **kwargs: Any) -> Any:
        response = real(method, url, token, **kwargs)
        # Only once ensure has made every change, when it reads them back.
        if (
            method == "GET"
            and ("PUT", OIDC.path) in [(m, path) for m, path, _ in gh.calls]
            and url.endswith(WORKFLOW.path)
        ):
            response.clear()
        return response

    assert _run(tmp_path, monkeypatch, request, ["ensure"]) == 1
    assert capsys.readouterr().err.startswith("Updated o/r, but couldn't check GitHub applied every change: ")


def test_no_settings_defined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(MATCHING)
    assert _run(tmp_path, monkeypatch, gh, ["check"], "[project]\nname = 'x'\n") == 1
    assert gh.calls == []
    assert "No Actions settings are defined" in capsys.readouterr().err


@pytest.mark.parametrize("code", [403, 404, 409])
def test_http_error_fails_even_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    def request(*args: object, **kwargs: object) -> Any:
        raise _http_error(code)

    # --exit-zero forgives settings needing ensure, but not an error.
    assert _run(tmp_path, monkeypatch, request, ["check", "--exit-zero"]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"HTTP {code} from https://x:\nnope")
    assert ("organization policy" in err) == (code == 409)
    assert ("doesn't apply to the repository" in err) == (code == 404)


TABLE = "[tool.repoutils.actions-settings]\n"


@pytest.mark.parametrize(
    ("config_text", "error"),
    [
        ("[tool.repoutils]\nactions-settings = 1\n", "must be a table"),
        (TABLE + "allowed = 'all'\n", "unknown setting: allowed"),
        (TABLE + "oidc = true\n", "unknown setting: oidc"),
        (TABLE + "oidc = { immutable = true }\n", "unknown setting: oidc.immutable"),
        (TABLE + "allowed-actions = 'local_only'\n", 'allowed-actions must be one of "all", "owner"'),
        (TABLE + "require-sha-pinning = 1\n", "require-sha-pinning must be true or false"),
        (TABLE + "retention-days = 0\n", "retention-days must be a positive integer"),
        (TABLE + "retention-days = true\n", "retention-days must be a positive integer"),
        (TABLE + "cache-size-gb = '10'\n", "cache-size-gb must be a positive integer"),
        (
            TABLE + "allowed-actions = 'selected'\nselected-actions.patterns = ['a', 'a']\n",
            "selected-actions.patterns must be a list of different non-empty strings",
        ),
        (
            TABLE + "allowed-actions = 'selected'\nselected-actions.patterns = ['']\n",
            "selected-actions.patterns must be a list",
        ),
        (TABLE + "selected-actions.github-owned = true\n", 'selected-actions needs allowed-actions = "selected"'),
        (
            TABLE + "allowed-actions = 'all'\nselected-actions.github-owned = true\n",
            'selected-actions needs allowed-actions = "selected"',
        ),
        (TABLE + "oidc.include-claim-keys = ['repo']\n", "oidc.include-claim-keys needs oidc.use-default-subject"),
    ],
)
def test_bad_config(tmp_path: Path, config_text: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config_text)
    with pytest.raises(config.ConfigError, match=re.escape(error)):
        actions_settings.load_settings(tmp_path)


def test_bad_config_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(MATCHING)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], TABLE + "retention-days = -1\n") == 1
    assert gh.calls == []
    assert "retention-days must be a positive integer" in capsys.readouterr().err


def test_every_setting_has_a_distinct_key_and_field() -> None:
    settings = actions_settings.SETTINGS
    fields = {(s.endpoint, s.api_field) for s in settings}
    assert len({s.key for s in settings}) == len(fields) == len(settings)
    assert all(s.api_field in s.endpoint.fields for s in settings)


# Every setting, as GitHub returns it.
EVERYTHING = MATCHING | {
    "actions/permissions/workflow": {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": True},
    "actions/oidc/customization/sub": {
        "use_default": False,
        "include_claim_keys": ["repo", "context"],
        "use_immutable_subject": False,
    },
}


def test_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # dump doesn't need pyproject.toml to set anything.
    assert _run(tmp_path, monkeypatch, FakeGitHub(EVERYTHING), ["dump"], "") == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out == (
        "[tool.repoutils.actions-settings]\n"
        'allowed-actions = "selected"\n'
        "require-sha-pinning = false\n"
        'default-token-permissions = "write"\n'
        "can-approve-pull-requests = true\n"
        'fork-pr-approval = "all-external-contributors"\n'
        "retention-days = 30\n"
        "cache-retention-days = 7\n"
        "cache-size-gb = 10\n"
        "\n"
        "[tool.repoutils.actions-settings.selected-actions]\n"
        "github-owned = true\n"
        "verified-creators = false\n"
        'patterns = ["extractions/setup-just@*", "astral-sh/*"]\n'
        "\n"
        "[tool.repoutils.actions-settings.oidc]\n"
        "use-default-subject = false\n"
        'include-claim-keys = ["repo", "context"]\n'
        "immutable-subject = false\n"
    )
    # Pasted into pyproject.toml, it gives every setting as GitHub has it.
    gh = FakeGitHub(EVERYTHING)
    assert _run(tmp_path, monkeypatch, gh, ["check"], out) == 0
    assert capsys.readouterr().out == f"All {len(BY_KEY)} setting(s) in o/r match pyproject.toml.\n"
    assert gh.writes() == []


def test_dump_leaves_out_what_it_cant_give(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = {path: response for path, response in MATCHING.items() if "cache" not in path} | {
        PERMISSIONS.path: {"enabled": True, "allowed_actions": "local_only"},
        "actions/permissions/fork-pr-contributor-approval": {"approval_policy": "new"},
        "actions/permissions/artifact-and-log-retention": {"days": 0},
        OIDC.path: {"use_default": True, "include_claim_keys": ["repo"]},
    }
    # dump neither reads pyproject.toml (this one is malformed) nor changes it.
    config_text = "[tool.repoutils]\nactions-settings = 1\n"
    gh = FakeGitHub(existing)
    assert _run(tmp_path, monkeypatch, gh, ["dump"], config_text) == 0
    assert (tmp_path / "pyproject.toml").read_text() == config_text
    # The selected actions aren't in effect, so it doesn't ask for them.
    assert ("GET", SELECTED.path, None) not in gh.calls
    out, err = capsys.readouterr()
    assert out == (
        "[tool.repoutils.actions-settings]\n"
        'allowed-actions = "owner"\n'
        'default-token-permissions = "read"\n'
        "can-approve-pull-requests = false\n"
        "\n"
        "[tool.repoutils.actions-settings.oidc]\n"
        "use-default-subject = true\n"
    )
    assert err == (
        "Left out require-sha-pinning: GitHub didn't return sha_pinning_required\n"
        "Left out fork-pr-approval: GitHub's value 'new' isn't one [tool.repoutils.actions-settings] can give\n"
        "Left out retention-days: GitHub's value 0 isn't one [tool.repoutils.actions-settings] can give\n"
        "Left out oidc.immutable-subject: GitHub didn't return use_immutable_subject\n"
        "Left out the settings from actions/cache/retention-limit: GitHub returned 404\n"
        "Left out the settings from actions/cache/storage-limit: GitHub returned 404\n"
    )
    assert _run(tmp_path, monkeypatch, FakeGitHub(existing), ["check"], out) == 0


@pytest.mark.parametrize("code", [403, 404])
def test_dump_fails_on_other_http_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], code: int
) -> None:
    # A 404 from the first endpoint means the repository's name is wrong, or the token can't see it.
    def request(*args: object, **kwargs: object) -> Any:
        raise _http_error(code)

    assert _run(tmp_path, monkeypatch, request, ["dump"], "") == 1
    assert capsys.readouterr().err.startswith(f"HTTP {code} from https://x:")


def test_dump_with_actions_disabled() -> None:
    text, skipped = actions_settings.dump_toml({PERMISSIONS: {"enabled": False}, SELECTED: None})
    assert text == '[tool.repoutils.actions-settings]\nallowed-actions = "disabled"\n'
    assert skipped == ["require-sha-pinning: GitHub didn't return sha_pinning_required"]


def test_toml_value_writes_numbers_and_lists() -> None:
    assert config.toml_value(3) == "3"
    assert config.toml_value(["a", 'b"']) == '["a", "b\\""]'


def test_dump_gives_selected_actions_only_with_allowed_actions() -> None:
    # Were allowed-actions left out, check would refuse the selected actions.
    responses = {PERMISSIONS: {"enabled": True, "allowed_actions": 1}, SELECTED: MATCHING[SELECTED.path]}
    text, _ = actions_settings.dump_toml(responses)
    assert "selected" not in text


def test_gh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def gh_output(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "gh_output", gh_output)
    assert actions_settings.main(["check"]) == 1
    assert capsys.readouterr().err == "gh auth token failed:\nnot logged in\n"


def test_a_field_github_leaves_out_is_a_difference() -> None:
    # With the default template, GitHub may leave out the claim keys.
    settings = {BY_KEY["oidc.use-default-subject"]: False, BY_KEY["oidc.include-claim-keys"]: ["repo"]}
    body, changes = actions_settings.diff(settings, {"use_default": True})
    assert body == {"use_default": False, "include_claim_keys": ["repo"]}
    assert changes == [
        "oidc.use-default-subject is True on GitHub, not False",
        "oidc.include-claim-keys is unset on GitHub, not ['repo']",
    ]


def test_dump_doesnt_miss_claim_keys_with_the_default_template() -> None:
    text, skipped = actions_settings.dump_toml({OIDC: {"use_default": True, "use_immutable_subject": True}})
    assert "use-default-subject = true\n" in text
    assert skipped == []

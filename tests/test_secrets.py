import re
import subprocess
import tomllib
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import config, github
from repoutils.commands import secrets

CONFIG = """
[tool.repoutils.secrets]
actions = ["CODECOV_TOKEN", "autofix_token"]
dependabot = ["DEPENDABOT_FIX_TOKEN"]
"""


class FakeGitHub:
    """Answers github.request like the secrets APIs of o/r would, with the given names."""

    def __init__(
        self, actions: list[str], dependabot: list[str], environments: dict[str, list[str]] | None = None
    ) -> None:
        self.existing = {"actions/secrets": actions, "dependabot/secrets": dependabot}
        self.environments = environments or {}
        for name, names in self.environments.items():
            self.existing[f"environments/{urllib.parse.quote(name, safe='')}/secrets"] = names
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, token: str, **kwargs: object) -> Any:
        assert token == "tok"
        assert method == "GET"
        self.calls.append((method, url))
        path, _, query = url.removeprefix(f"{github.API_URL}/repos/o/r/").partition("?")
        prefix = f"per_page={secrets.PER_PAGE}&page="
        assert query.startswith(prefix)
        page = int(query.removeprefix(prefix))
        if path == "environments":
            envs = list(self.environments)[(page - 1) * secrets.PER_PAGE : page * secrets.PER_PAGE]
            return {"total_count": len(self.environments), "environments": [{"name": name} for name in envs]}
        if path not in self.existing:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        names = self.existing[path]
        batch = names[(page - 1) * secrets.PER_PAGE : page * secrets.PER_PAGE]
        return {
            "total_count": len(names),
            "secrets": [{"name": name, "created_at": "", "updated_at": ""} for name in batch],
        }


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHub, argv: list[str], config_text: str = CONFIG
) -> int:
    (tmp_path / "pyproject.toml").write_text(config_text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    return secrets.main([*argv, "--repo", "o/r"])


ALL = FakeGitHub(["AUTOFIX_TOKEN", "CODECOV_TOKEN", "EXTRA"], ["DEPENDABOT_FIX_TOKEN"])


def test_check_passes_ignoring_case_and_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, monkeypatch, ALL, ["check"]) == 0
    assert capsys.readouterr().out == "All 3 secret(s) pyproject.toml lists exist in o/r.\n"


def test_check_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(["CODECOV_TOKEN"], [])
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert capsys.readouterr().out == (
        "Actions secret autofix_token: missing\n"
        "Dependabot secret DEPENDABOT_FIX_TOKEN: missing\n"
        "2 of 3 secret(s) are missing from o/r: "
        "set them with `gh secret set` (`--app dependabot` for a Dependabot secret, "
        "`--env NAME` for an environment's).\n"
    )


def test_check_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub([], []), ["check", "--exit-zero"]) == 0
    assert "3 of 3 secret(s) are missing" in capsys.readouterr().out


def test_check_fail_on_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(["AUTOFIX_TOKEN", "EXTRA"], ["DEPENDABOT_FIX_TOKEN", "OTHER"])
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"]) == 1
    assert capsys.readouterr().out == (
        "Actions secret CODECOV_TOKEN: missing\n"
        "Actions secret EXTRA: not listed in pyproject.toml\n"
        "Dependabot secret OTHER: not listed in pyproject.toml\n"
        "1 of 3 secret(s) are missing from o/r: "
        "set them with `gh secret set` (`--app dependabot` for a Dependabot secret, "
        "`--env NAME` for an environment's).\n"
        "2 secret(s) in o/r aren't listed in pyproject.toml: "
        "add them there (`repoutils secrets dump` prints them) or delete them on GitHub.\n"
    )


def test_omitted_kind_is_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(["A"], ["UNLISTED"])
    config_text = '[tool.repoutils.secrets]\nactions = ["A"]\n'
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], config_text) == 0
    assert [url for _, url in gh.calls] == [f"{github.API_URL}/repos/o/r/actions/secrets?per_page=100&page=1"]
    assert (
        capsys.readouterr().out
        == "All 1 secret(s) pyproject.toml lists exist in o/r, and it has no others of those kinds.\n"
    )


def test_empty_list_means_none_with_fail_on_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([], ["UNLISTED"])
    config_text = "[tool.repoutils.secrets]\ndependabot = []\n"
    assert _run(tmp_path, monkeypatch, gh, ["check"], config_text) == 0
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], config_text) == 1
    assert "Dependabot secret UNLISTED: not listed" in capsys.readouterr().out


def test_paginates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    many = [f"S{i:03}" for i in range(secrets.PER_PAGE + 1)]
    gh = FakeGitHub(many, [])
    config_text = f'[tool.repoutils.secrets]\nactions = ["{many[-1]}"]\n'
    assert _run(tmp_path, monkeypatch, gh, ["check"], config_text) == 0
    assert len(gh.calls) == 2


def test_dump_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(["ZED", "ALPHA"], [])
    assert _run(tmp_path, monkeypatch, gh, ["dump"], "") == 0
    out = capsys.readouterr().out
    assert out == (
        '[tool.repoutils.secrets]\nactions = [\n    "ALPHA",\n    "ZED",\n]\ndependabot = []\nenvironments = {}\n'
    )
    assert tomllib.loads(out)["tool"]["repoutils"]["secrets"] == {
        "actions": ["ALPHA", "ZED"],
        "dependabot": [],
        "environments": {},
    }
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], out) == 0
    assert (
        capsys.readouterr().out
        == "All 2 secret(s) pyproject.toml lists exist in o/r, and it has no others of those kinds.\n"
    )


def test_no_secrets_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub([], [])
    assert _run(tmp_path, monkeypatch, gh, ["check"], "") == 1
    assert gh.calls == []
    assert "No secrets are listed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("config_text", "error"),
    [
        ("[tool.repoutils]\nsecrets = 1\n", "[tool.repoutils.secrets] must be a table"),
        ("[tool.repoutils.secrets]\ncodespaces = []\n", "unknown setting(s): codespaces"),
        ("[tool.repoutils.secrets]\nactions = 'A'\n", "actions must be a list of strings"),
        ("[tool.repoutils.secrets]\nactions = ['1A']\n", "'1A' isn't a valid secret name"),
        ("[tool.repoutils.secrets]\nactions = ['A-B']\n", "'A-B' isn't a valid secret name"),
        ("[tool.repoutils.secrets]\nactions = ['github_token']\n", "'github_token' isn't a valid secret name"),
        ("[tool.repoutils.secrets]\nactions = ['A', 'a']\n", "'a' is listed twice"),
    ],
)
def test_load_secrets_errors(tmp_path: Path, config_text: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config_text)
    with pytest.raises(config.ConfigError, match=re.escape(error)):
        secrets.load_secrets(tmp_path)


def test_same_name_in_both_kinds_is_fine(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.repoutils.secrets]\nactions = ['A']\ndependabot = ['A']\n")
    assert secrets.load_secrets(tmp_path) == {"actions": ["A"], "dependabot": ["A"]}


def test_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail(method: str, url: str, token: str, **kwargs: object) -> Any:
        raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), None)

    monkeypatch.setattr(github, "request", fail)
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    # --exit-zero ignores missing secrets, but not an error.
    assert secrets.main(["check", "--exit-zero", "--repo", "o/r"]) == 1
    assert "HTTP 403 from" in capsys.readouterr().err


def test_gh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def gh_output(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    monkeypatch.setattr(github, "gh_output", gh_output)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    assert secrets.main(["dump", "--repo", "o/r"]) == 1
    assert "gh auth token failed:\nnot logged in" in capsys.readouterr().err


def test_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.repoutils.secrets]\nactions = [1]\n")
    monkeypatch.chdir(tmp_path)
    assert secrets.main(["check"]) == 1
    assert "must be a list of strings" in capsys.readouterr().err


def _workflows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_text: str, **files: str) -> int:
    (tmp_path / "pyproject.toml").write_text(config_text)
    directory = tmp_path / ".github" / "workflows"
    directory.mkdir(parents=True)
    for name, text in files.items():
        (directory / name.replace("_", ".")).write_text(text)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return secrets.main(["check-workflows"])


WORKFLOW = """
env:
  B: ${{ secrets['autofix_token'] }}
  C: ${{ secrets.GITHUB_TOKEN }}
  # D: ${{ secrets.COMMENTED_OUT }}
  E: plain  # not secrets.TRAILING_COMMENT
  F: "issue #1 ${{ secrets.CODECOV_TOKEN }}"  # after 'a quote' secrets.ALSO_A_COMMENT
"""


def test_check_workflows_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = 'env:\n  E: ${{ secrets[ "DEPENDABOT_FIX_TOKEN" ] }}\n  F: ${{ secrets.ORG_TOKEN }}\n'
    config_text = CONFIG + 'other = ["ORG_TOKEN"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=WORKFLOW, other_yaml=other) == 0
    assert capsys.readouterr().out == "The workflows use exactly the 4 secret(s) pyproject.toml lists.\n"
    # check doesn't look for the other secrets.
    assert secrets.load_secrets(tmp_path) == {
        "actions": ["CODECOV_TOKEN", "autofix_token"],
        "dependabot": ["DEPENDABOT_FIX_TOKEN"],
    }


def test_check_workflows_reports_unlisted_and_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = "env:\n  A: ${{ secrets.NEW_ONE }}\n"
    assert _workflows(tmp_path, monkeypatch, CONFIG, ci_yml=WORKFLOW, a_yml=extra, b_yml=extra) == 1
    assert capsys.readouterr().out == (
        "NEW_ONE: used by a.yml, b.yml, but not listed in pyproject.toml\n"
        "DEPENDABOT_FIX_TOKEN: listed in pyproject.toml, but no workflow uses it\n"
        "1 secret(s) the workflows use aren't listed in pyproject.toml: add each to [tool.repoutils.secrets], "
        "under actions, dependabot or environments.<name>, or under other if the repository doesn't hold it.\n"
        "1 secret(s) pyproject.toml lists aren't used by any workflow: remove them from [tool.repoutils.secrets].\n"
    )


def test_check_workflows_needs_no_table_or_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert secrets.main(["check-workflows"]) == 0
    assert capsys.readouterr().out == "The workflows use exactly the 0 secret(s) pyproject.toml lists.\n"


def test_other_alone_is_not_enough_for_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([], [])
    assert _run(tmp_path, monkeypatch, gh, ["check"], '[tool.repoutils.secrets]\nother = ["ORG_TOKEN"]\n') == 1
    assert gh.calls == []
    assert "No secrets are listed" in capsys.readouterr().err


ENV_CONFIG = """
[tool.repoutils.secrets]
environments.release = ["DEPLOY_KEY", "gh_actions_token"]
environments.autofix = ["AUTOFIX_TOKEN"]
"""


def test_check_environments_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(
        ["UNCHECKED"], [], {"Release": ["GH_ACTIONS_TOKEN", "DEPLOY_KEY"], "autofix": ["AUTOFIX_TOKEN"], "pypi": []}
    )
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], ENV_CONFIG) == 0
    assert (
        capsys.readouterr().out
        == "All 3 secret(s) pyproject.toml lists exist in o/r, and it has no others of those kinds.\n"
    )
    # Neither repository kind is listed, so neither is fetched.
    assert not any("actions/secrets" in url or "dependabot" in url for _, url in gh.calls)


def test_check_environments_reports_missing_and_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([], [], {"release": ["DEPLOY_KEY", "EXTRA"], "my env": ["STRAY"], "pypi": []})
    assert _run(tmp_path, monkeypatch, gh, ["check"], ENV_CONFIG) == 1
    assert capsys.readouterr().out == (
        "Environment release secret gh_actions_token: missing\n"
        "Environment autofix: missing, with its 1 secret(s)\n"
        "1 environment(s) are missing from o/r: "
        "create them with `repoutils environments ensure` or on the repository's settings page.\n"
        "2 of 3 secret(s) are missing from o/r: "
        "set them with `gh secret set` (`--app dependabot` for a Dependabot secret, "
        "`--env NAME` for an environment's).\n"
    )
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], ENV_CONFIG) == 1
    out = capsys.readouterr().out
    assert "Environment release secret EXTRA: not listed in pyproject.toml\n" in out
    # An environment pyproject.toml doesn't list may have no secrets.
    assert "Environment my env secret STRAY: not listed in pyproject.toml\n" in out
    assert "pypi" not in out
    assert "2 secret(s) in o/r aren't listed in pyproject.toml" in out


def test_check_missing_environment_alone_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_text = "[tool.repoutils.secrets]\nenvironments.release = []\n"
    assert _run(tmp_path, monkeypatch, FakeGitHub([], []), ["check"], config_text) == 1
    assert capsys.readouterr().out.startswith("Environment release: missing\n1 environment(s) are missing")
    assert _run(tmp_path, monkeypatch, FakeGitHub([], []), ["check", "--exit-zero"], config_text) == 0


def test_check_no_environments_means_none_with_fail_on_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([], [], {"release": ["DEPLOY_KEY"]})
    config_text = "[tool.repoutils.secrets]\nenvironments = {}\n"
    assert _run(tmp_path, monkeypatch, gh, ["check"], config_text) == 0
    assert capsys.readouterr().out == "All 0 secret(s) pyproject.toml lists exist in o/r.\n"
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], config_text) == 1
    assert "Environment release secret DEPLOY_KEY: not listed" in capsys.readouterr().out


def test_environments_unlisted_are_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(["AUTOFIX_TOKEN", "CODECOV_TOKEN"], ["DEPENDABOT_FIX_TOKEN"], {"release": ["STRAY"]})
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"]) == 0
    assert not any("environments" in url for _, url in gh.calls)


def test_environment_secrets_error_fails_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([], [], {"release": []})
    # The environment is listed, but its secrets can't be read.
    del gh.existing["environments/release/secrets"]
    config_text = "[tool.repoutils.secrets]\nenvironments.release = []\n"
    assert _run(tmp_path, monkeypatch, gh, ["check", "--exit-zero"], config_text) == 1
    assert "HTTP 404 from" in capsys.readouterr().err


def test_dump_environments_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(["A"], [], {"release": ["Z", "B"], "my env": ["C"], "pypi": []})
    assert _run(tmp_path, monkeypatch, gh, ["dump"], "") == 0
    out = capsys.readouterr().out
    assert out == (
        "[tool.repoutils.secrets]\n"
        'actions = [\n    "A",\n]\n'
        "dependabot = []\n"
        'environments."my env" = [\n    "C",\n]\n'
        'environments.release = [\n    "B",\n    "Z",\n]\n'
    )
    assert f"{github.API_URL}/repos/o/r/environments/my%20env/secrets?per_page=100&page=1" in [
        url for _, url in gh.calls
    ]
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], out) == 0
    assert capsys.readouterr().out.startswith("All 4 secret(s)")


def test_environments_alone_are_enough_for_check(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(ENV_CONFIG)
    assert secrets.load_secrets(tmp_path) == {}
    assert secrets.load_environment_secrets(tmp_path) == {
        "release": ["DEPLOY_KEY", "gh_actions_token"],
        "autofix": ["AUTOFIX_TOKEN"],
    }
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    assert secrets.load_environment_secrets(tmp_path) is None


@pytest.mark.parametrize(
    ("config_text", "error"),
    [
        ("[tool.repoutils.secrets]\nenvironments = []\n", "environments must be a table of each environment's"),
        ("[tool.repoutils.secrets]\nenvironments.release = 'A'\n", "environments] release must be a list of strings"),
        ("[tool.repoutils.secrets]\nenvironments.release = ['1A']\n", "environments] release '1A' isn't a valid"),
        ("[tool.repoutils.secrets]\nenvironments.release = ['A', 'a']\n", "release 'a' is listed twice"),
        (
            "[tool.repoutils.secrets]\nenvironments.release = []\nenvironments.Release = []\n",
            "environments] Release is listed twice (environment names ignore case)",
        ),
    ],
)
def test_load_environment_secrets_errors(tmp_path: Path, config_text: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config_text)
    with pytest.raises(config.ConfigError, match=re.escape(error)):
        secrets.load_environment_secrets(tmp_path)


ENV_WORKFLOW = """
on: push
jobs:
  release:
    runs-on: ubuntu-latest
    environment: release
    steps:
      - run: echo ${{ secrets.DEPLOY_KEY }}
  bump:  # a comment
    environment:
      name: 'release'
      url: https://example.com/
    steps:
      - env:
          T: ${{ secrets.GH_ACTIONS_TOKEN }}
        run: |
          environment: not-this-one
  fix:
    environment: { name: "autofix", url: "https://example.com/" }
    env:
      T: ${{ secrets.AUTOFIX_TOKEN }}
  dynamic:
    environment: ${{ inputs.environment }}
    env:
      T: ${{ secrets.AUTOFIX_TOKEN }}
  test:
    env:
      T: ${{ secrets.CODECOV_TOKEN }}
"""


def test_check_workflows_environment_secrets_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_text = ENV_CONFIG.replace("gh_actions_token", "GH_ACTIONS_TOKEN") + 'actions = ["CODECOV_TOKEN"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=ENV_WORKFLOW) == 0
    assert capsys.readouterr().out == "The workflows use exactly the 4 secret(s) pyproject.toml lists.\n"


def test_check_workflows_reports_job_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = ENV_WORKFLOW + "  other:\n    environment: autofix\n    env:\n      T: ${{ secrets.DEPLOY_KEY }}\n"
    workflow += "  bare:\n    steps:\n      - run: echo ${{ secrets.DEPLOY_KEY }} ${{ secrets.CODECOV_TOKEN }}\n"
    config_text = ENV_CONFIG + 'actions = ["CODECOV_TOKEN"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=workflow) == 1
    assert capsys.readouterr().out == (
        "DEPLOY_KEY: used by job other in ci.yml, which declares environment autofix, "
        "but only environment release holds it\n"
        "DEPLOY_KEY: used by job bare in ci.yml, which declares no environment, "
        "but only environment release holds it\n"
        "2 use(s) of an environment's secret are in a job that doesn't deploy to it, "
        "so the secret is empty there: give the job that environment.\n"
    )


def test_check_workflows_repository_secret_needs_no_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A secret the repository holds too is set in a job without the environment.
    workflow = (
        "jobs:\n  a:\n    env:\n      T: ${{ secrets.TOKEN }}\n"
        "  b:\n    environment: x\n    env:\n      T: ${{ secrets.TOKEN }}\n"
    )
    config_text = '[tool.repoutils.secrets]\nactions = ["TOKEN"]\nenvironments.x = ["TOKEN"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=workflow) == 0
    assert capsys.readouterr().out == "The workflows use exactly the 1 secret(s) pyproject.toml lists.\n"


def test_check_workflows_reads_an_escaped_quote_in_an_environment_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = "jobs:\n  a:\n    environment: {name: 'prod''us'}\n    env:\n      T: ${{ secrets.TOKEN }}\n"
    config_text = '[tool.repoutils.secrets]\nenvironments.prod = ["TOKEN"]\n'
    assert _workflows(tmp_path, monkeypatch, config_text, ci_yml=workflow) == 1
    assert "TOKEN: used by job a in ci.yml, which declares environment prod'us" in capsys.readouterr().out

"""Runs each command through ``repoutils <command>``, with arguments for which
it returns rather than exits, and which need no network and change nothing, so
every command's ``run()`` is covered and a mistake in it (e.g. dropping the
exit code) fails a test."""

import io
import json
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from repoutils import cli
from repoutils.commands.environments import load_environments
from repoutils.commands.labels import load_labels
from repoutils.commands.repo_settings import load_settings
from repoutils.commands.secrets import load_secrets
from repoutils.ruleset_format import load_rulesets

ROOT = Path(__file__).parents[1]


def test_help_lists_every_command_by_group(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "Repo hygiene:" in out
    for name in cli.COMMANDS:
        assert f"    {name} " in out


def test_no_arguments_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage: repoutils <command>" in capsys.readouterr().out


def test_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["nope"]) == 2
    assert "unknown command 'nope'" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("repoutils ")


def test_every_command_module_has_run() -> None:
    for name in cli.COMMANDS:
        assert callable(cli.load(name)), name


@pytest.mark.parametrize("name", sorted(cli.COMMANDS))
def test_every_command_has_help(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main([name, "--help"])
    assert exit_info.value.code == 0
    assert f"usage: repoutils {name}" in capsys.readouterr().out


def test_python_dash_m(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "repoutils", "--version"], capture_output=True, text=True, check=False, cwd=tmp_path
    )
    assert (result.returncode, result.stdout.split()[0]) == (0, "repoutils")


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["check-checkout-credentials"], "set persist-credentials"),
        (["check-pins"], "pin uv"),
        (["check-prose", "--exit-zero"], "sentences"),
        (["check-sentence-per-line"], "one-sentence-per-line"),
        (["check-whitespace"], "clean whitespace"),
        (["long-files", "--base", ""], "500 lines"),
        (["requirements-min-versions", "check"], "Dependency minimum versions match uv.lock."),
        (["check-lock-platforms"], "uv.lock"),
    ],
)
def test_repo_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: list[str], expected: str
) -> None:
    # They check the repo in the current directory: this one.
    monkeypatch.chdir(ROOT)
    assert cli.main(args) == 0
    assert expected in capsys.readouterr().out


def test_check_blame_ignore_revs(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # This repo has no .git-blame-ignore-revs, which the check reports.
    monkeypatch.chdir(ROOT)
    assert cli.main(["check-blame-ignore-revs", "no-such-ref"]) == 1
    assert "Problems in .git-blame-ignore-revs" in capsys.readouterr().out


def test_check_screenshots_dir(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # This repo has no screenshots directory, which the check reports.
    monkeypatch.chdir(ROOT)
    assert cli.main(["check-screenshots-dir", "--dir", "no-such-dir"]) == 1
    assert "no-such-dir/.gitignore isn't tracked" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "usage"),
    [
        (["check-blame-ignore-revs", "a", "b"], "[target]"),
        (["dependabot-relock"], "base-ref"),
        (["requirements-min-versions", "neither"], "{check,fix}"),
        (["check-attestations", "a", "b"], "[dist]"),
        (["labels"], "{check,ensure,dump}"),
        (["repo-settings"], "{check,ensure,dump}"),
        (["secrets"], "{check,dump,check-workflows}"),
        (["environments"], "{check,ensure,dump}"),
        (["rulesets"], "{check,ensure,dump}"),
    ],
)
def test_usage_errors(capsys: pytest.CaptureFixture[str], args: list[str], usage: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(args)
    assert exit_info.value.code == 2
    assert usage in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["post-comment", "1", "--name", "x"],
        ["post-patch-coverage", "1", "--report"],
        ["post-screenshots", "1", "--dir"],
    ],
)
def test_comment_posters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    # A missing report fails before anything talks to GitHub.
    monkeypatch.setenv("GITHUB_TOKEN", "unused")
    monkeypatch.setenv("SCREENSHOTS_TOKEN", "unused")
    assert cli.main([*args, str(tmp_path / "missing")]) == 1
    assert "missing" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "config_text", "message", "needs_lock"),
    [
        (
            ["check-prose"],
            "[tool.repoutils.prose]\nfiles = 'README.md'\n",
            "[tool.repoutils.prose] files must be a list of strings",
            False,
        ),
        (
            ["check-prose", "--exit-zero"],
            "[tool.repoutils.prose]\nfiles = 'README.md'\n",
            "[tool.repoutils.prose] files must be a list of strings",
            False,
        ),
        (
            ["check-sentence-per-line"],
            "[tool.repoutils.sentence-per-line]\nfiles = 3\n",
            "[tool.repoutils.sentence-per-line] files must be a list of strings",
            False,
        ),
        (
            ["check-screenshots-dir"],
            "[tool.repoutils.screenshots]\ndir = 3\n",
            "[tool.repoutils.screenshots] dir must be a non-empty string",
            False,
        ),
        (
            ["check-lock-platforms"],
            "[tool.repoutils.lock-platforms]\nsupported = 3\n",
            "[tool.repoutils.lock-platforms] supported must be a list of strings",
            True,
        ),
        (
            ["post-comment", "1", "report.md", "--name", "x"],
            "[tool.repoutils]\ncomment-prefix = 3\n",
            "[tool.repoutils] comment-prefix must be a non-empty string without '-->'",
            False,
        ),
        (
            ["post-patch-coverage", "1"],
            "[tool.repoutils]\ncomment-prefix = 3\n",
            "[tool.repoutils] comment-prefix must be a non-empty string without '-->'",
            False,
        ),
        (
            ["post-screenshots", "1"],
            "[tool.repoutils]\ncomment-prefix = 3\n",
            "[tool.repoutils] comment-prefix must be a non-empty string without '-->'",
            False,
        ),
    ],
)
def test_bad_pyproject_setting_prints_one_line_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    config_text: str,
    message: str,
    needs_lock: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(config_text)
    if needs_lock:
        (tmp_path / "uv.lock").write_text("version = 1\n")
    assert cli.main(args) == 1
    assert capsys.readouterr().err == f"{message}\n"


@pytest.mark.parametrize(
    ("args", "needs_lock"),
    [
        (["check-prose"], False),
        (["check-prose", "--exit-zero"], False),
        (["check-sentence-per-line"], False),
        (["check-screenshots-dir"], False),
        (["check-lock-platforms"], True),
        (["post-comment", "1", "report.md", "--name", "x"], False),
        (["post-patch-coverage", "1"], False),
        (["post-screenshots", "1"], False),
        # These read other tables of pyproject.toml, not [tool.repoutils].
        (["check-pins"], False),
        (["requirements-min-versions", "check"], True),
    ],
)
@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"[tool.repoutils\n", "pyproject.toml: Expected ']' at the end of a table declaration (at line 1, column 16)"),
        (b'[tool.repoutils]\nx = "\xff"\n', "pyproject.toml: not valid UTF-8"),
    ],
)
def test_unparsable_pyproject_prints_one_line_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    needs_lock: bool,
    content: bytes,
    message: str,
) -> None:
    # The message names the file, and --exit-zero doesn't forgive it.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(content)
    if needs_lock:
        (tmp_path / "uv.lock").write_text("version = 1\n")
    assert cli.main(args) == 1
    assert capsys.readouterr().err == f"{message}\n"


def test_other_value_error_keeps_its_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only a ConfigError is the project's mistake: any other ValueError is a bug, so it isn't swallowed.
    def run(argv: list[str]) -> int:
        raise ValueError("a bug")

    monkeypatch.setattr(cli, "load", lambda name: run)
    with pytest.raises(ValueError, match="a bug"):
        cli.main(["check-prose"])


def test_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # A project with no labels fails before anything talks to GitHub.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["labels", "check"]) == 1
    assert "No labels are defined" in capsys.readouterr().err


def test_repo_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # A project with no settings fails before anything talks to GitHub.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["repo-settings", "check"]) == 1
    assert "No repository settings are defined" in capsys.readouterr().err


def test_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # A project with no secrets listed fails before anything talks to GitHub.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["secrets", "check"]) == 1
    assert "No secrets are listed" in capsys.readouterr().err


def test_this_repos_secrets_match_its_workflows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(ROOT)
    assert cli.main(["secrets", "check-workflows"]) == 0
    assert "The workflows use exactly the" in capsys.readouterr().out
    assert load_secrets(ROOT)["dependabot"] == ["DEPENDABOT_FIX_TOKEN"]


def test_environments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # A project with no environments fails before anything talks to GitHub.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["environments", "check"]) == 1
    assert "No environments are defined" in capsys.readouterr().err


def test_this_repos_environments_load() -> None:
    # Only a v* tag may deploy to pypi (issue #3).
    by_name = {environment.name: environment.settings for environment in load_environments(ROOT)}
    assert by_name["pypi"]["allowed-tags"] == ["v*"]
    assert by_name["testpypi"]["deployment-branches"] == "all"


def test_rulesets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # A project with no rulesets fails before anything talks to GitHub.
    monkeypatch.chdir(tmp_path)
    assert cli.main(["rulesets", "check"]) == 1
    assert "No rulesets are defined" in capsys.readouterr().err


def test_this_repos_rulesets_load() -> None:
    main = load_rulesets(ROOT)["main"]
    assert {"deletion", "non-fast-forward", "pull-request", "required-status-checks"} <= set(main["rules"])


def test_this_repos_settings_load() -> None:
    assert {setting.key for setting in load_settings(ROOT)} >= {"issues", "squash-merge.allow"}


def test_this_repos_labels_load() -> None:
    labels = load_labels(ROOT)
    assert {"dependencies", "type:maintenance"} <= {label.name for label in labels}


def test_check_attestations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "x-1.0-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "x-1.0-py3-none-any.whl.publish.attestation").write_text("{}")
    assert cli.main(["check-attestations", str(tmp_path)]) == 0
    assert "all 1 distributions" in capsys.readouterr().out


def test_claude_route(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("@claude review"))
    assert cli.main(["claude-route"]) == 0
    assert capsys.readouterr().out == "review\n"


def test_recreate_pr(capsys: pytest.CaptureFixture[str]) -> None:
    # An invalid URL fails before anything runs gh.
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["recreate-pr", "https://github.com/o/r/issues/1"])
    assert exit_info.value.code == 1
    assert "Invalid GitHub PR URL" in capsys.readouterr().err


def test_git_config_setup_outside_a_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert cli.main(["git-config-setup"]) == 0


class _Response(io.BytesIO):
    """A urlopen response: its body, and the status the commands read."""

    status = 200


def test_wait_for_base_coverage(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def urlopen(request: urllib.request.Request, **kwargs: Any) -> _Response:
        assert request.full_url.startswith("https://api.codecov.io/")
        return _Response(json.dumps({"totals": {"coverage": 98.5}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert cli.main(["wait-for-base-coverage", "o/r", "abc"]) == 0
    assert "Codecov has the base report for abc (coverage 98.5%)" in capsys.readouterr().out

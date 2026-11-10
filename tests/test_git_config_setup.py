import subprocess
from pathlib import Path
from unittest import mock

import pytest

from repoutils.commands import git_config_setup


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _setting(repo: Path) -> str:
    result = subprocess.run(
        ["git", "config", "--get", "--local", "blame.ignoreRevsFile"], cwd=repo, capture_output=True, text=True
    )
    return result.stdout.strip()


def test_new_git_sets_the_optional_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    with mock.patch.object(git_config_setup, "git_version", return_value=(2, 53)):
        assert git_config_setup.main(repo) == 0
        assert _setting(repo) == ":(optional).git-blame-ignore-revs"
        assert "Updated local repo git config setting" in capsys.readouterr().err
        # Idempotent and quiet the second time.
        assert git_config_setup.main(repo) == 0
    assert capsys.readouterr().err == ""


def test_old_git_sets_the_plain_file_only_if_it_exists(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with mock.patch.object(git_config_setup, "git_version", return_value=(2, 52)):
        assert git_config_setup.main(repo) == 0
        assert _setting(repo) == ""
        (repo / ".git-blame-ignore-revs").write_text("")
        assert git_config_setup.main(repo) == 0
    assert _setting(repo) == ".git-blame-ignore-revs"


def test_git_version_parses_and_tolerates_odd_output() -> None:
    assert git_config_setup.git_version() >= (2, 0)
    odd = subprocess.CompletedProcess(["git"], 0, stdout="something else", stderr="")
    with mock.patch.object(git_config_setup, "_git", return_value=odd):
        assert git_config_setup.git_version() == (0, 0)


def test_a_failing_config_read_is_an_error(tmp_path: Path) -> None:
    failed = subprocess.CompletedProcess(["git"], 3, stdout="", stderr="bad config")
    with mock.patch.object(git_config_setup, "_git", return_value=failed), pytest.raises(RuntimeError, match="bad"):
        git_config_setup.ensure_setting(tmp_path, "a.b", "c")

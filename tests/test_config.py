from pathlib import Path

import pytest

from repotools import config


def _write(root: Path, text: str) -> Path:
    (root / "pyproject.toml").write_text(text)
    return root


def test_missing_file_or_table_means_defaults(tmp_path: Path) -> None:
    assert config.load(tmp_path) == {}
    _write(tmp_path, "[project]\nname = 'x'\n")
    assert config.load(tmp_path) == {}
    assert config.section("long-files", tmp_path) == {}
    assert config.marker("long-files", tmp_path) == "<!-- repotools-long-files -->"
    assert config.default_branch(tmp_path) == "main"


def test_reads_settings(tmp_path: Path) -> None:
    _write(
        tmp_path,
        '[tool.repotools]\ncomment-prefix = "lifetracker"\ndefault-branch = "master"\n'
        '[tool.repotools.sentence-per-line]\nfiles = ["A.md"]\n',
    )
    assert config.marker("patch-coverage", tmp_path) == "<!-- lifetracker-patch-coverage -->"
    assert config.default_branch(tmp_path) == "master"
    table = config.section("sentence-per-line", tmp_path)
    assert config.string_list(table, "files", [], "where") == ["A.md"]
    assert config.string_list(table, "missing", ["d"], "where") == ["d"]


@pytest.mark.parametrize(
    ("text", "call", "message"),
    [
        ("[tool]\nrepotools = 1\n", lambda root: config.load(root), "must be a table"),
        ("[tool.repotools]\nlong-files = 1\n", lambda root: config.section("long-files", root), "must be a table"),
        ("[tool.repotools]\ncomment-prefix = ''\n", lambda root: config.comment_prefix(root), "comment-prefix"),
        ("[tool.repotools]\ncomment-prefix = 'a-->'\n", lambda root: config.comment_prefix(root), "comment-prefix"),
        ("[tool.repotools]\ndefault-branch = 1\n", lambda root: config.default_branch(root), "default-branch"),
        (
            "[tool.repotools.x]\nfiles = [1]\n",
            lambda root: config.string_list(config.section("x", root), "files", [], "[x]"),
            "files must be a list of strings",
        ),
    ],
)
def test_rejects_bad_settings(tmp_path: Path, text: str, call: object, message: str) -> None:
    _write(tmp_path, text)
    assert callable(call)
    with pytest.raises(config.ConfigError, match=message):
        call(tmp_path)

from pathlib import Path

import pytest

from repoutils import config


def _write(root: Path, text: str) -> Path:
    (root / "pyproject.toml").write_text(text)
    return root


def test_missing_file_or_table_means_defaults(tmp_path: Path) -> None:
    assert config.load(tmp_path) == {}
    _write(tmp_path, "[project]\nname = 'x'\n")
    assert config.load(tmp_path) == {}
    assert config.section("long-files", tmp_path) == {}
    assert config.marker("long-files", tmp_path) == "<!-- repoutils-long-files -->"
    assert config.default_branch(tmp_path) == "main"


def test_reads_settings(tmp_path: Path) -> None:
    _write(
        tmp_path,
        '[tool.repoutils]\ncomment-prefix = "lifetracker"\ndefault-branch = "master"\n'
        '[tool.repoutils.sentence-per-line]\nfiles = ["A.md"]\n',
    )
    assert config.marker("patch-coverage", tmp_path) == "<!-- lifetracker-patch-coverage -->"
    assert config.default_branch(tmp_path) == "master"
    table = config.section("sentence-per-line", tmp_path)
    assert config.string_list(table, "files", [], "where") == ["A.md"]
    assert config.string_list(table, "missing", ["d"], "where") == ["d"]


def test_numbers(tmp_path: Path) -> None:
    _write(tmp_path, "[tool.repoutils.x]\nlimit = 2.5\noff = false\n")
    table = config.section("x", tmp_path)
    assert config.number(table, "limit", 1, "[x]") == 2.5
    assert config.number(table, "missing", 1, "[x]") == 1
    assert config.integer(table, "missing", 3, "[x]") == 3
    assert config.optional_number(table, "limit", 1, "[x]") == 2.5
    assert config.optional_number(table, "off", 1, "[x]") is None


@pytest.mark.parametrize(
    ("text", "call", "message"),
    [
        ("[tool]\nrepoutils = 1\n", lambda root: config.load(root), "must be a table"),
        ("tool = 1\n", lambda root: config.load(root), r"\[tool\] must be a table"),
        ("[tool.repoutils]\nlong-files = 1\n", lambda root: config.section("long-files", root), "must be a table"),
        ("[tool.repoutils]\ncomment-prefix = ''\n", lambda root: config.comment_prefix(root), "comment-prefix"),
        ("[tool.repoutils]\ncomment-prefix = 'a-->'\n", lambda root: config.comment_prefix(root), "comment-prefix"),
        ("[tool.repoutils]\ndefault-branch = 1\n", lambda root: config.default_branch(root), "default-branch"),
        (
            "[tool.repoutils.x]\nlimit = true\n",
            lambda root: config.number(config.section("x", root), "limit", 1, "[x]"),
            r"\[x\] limit must be a number, 0 or more$",
        ),
        (
            "[tool.repoutils.x]\nlimit = 2.5\n",
            lambda root: config.integer(config.section("x", root), "limit", 1, "[x]"),
            r"\[x\] limit must be a whole number, 0 or more$",
        ),
        (
            "[tool.repoutils.x]\nlimit = -1\n",
            lambda root: config.optional_number(config.section("x", root), "limit", 1, "[x]"),
            "limit must be a number, 0 or more, or false",
        ),
        (
            "[tool.repoutils.x]\nfiles = [1]\n",
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

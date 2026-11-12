from pathlib import Path

import pytest

from repoutils import config
from repoutils.commands import check_prose

PLAIN = "The tool reads the file and prints what it finds.\n" * 20


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _configure(project: Path, settings: str) -> None:
    (project / "pyproject.toml").write_text(f"[tool.repoutils.prose]\n{settings}")


def test_plain_prose_passes(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "README.md").write_text(PLAIN)
    assert check_prose.main() == 0
    assert capsys.readouterr().out == "README.md: 20 sentences, within the limits.\n"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("This one sentence has a great many words in it and goes on for much longer than it should.\n", "words per"),
        (
            "Short one here.\nShort one there.\nThis sentence, however, " + "goes on and on " * 10 + "to the end.\n",
            "33.33 % of sentences over 30 words (limit 12)",
        ),
        ("Red, green, blue and pink are colors.\nSo are black, white and grey.\n", "1.50 commas per sentence"),
        ("It runs; it stops.\nIt waits here.\n", "0.50 semicolons per sentence (limit 0.05)"),
        ("It runs, and it stops, but it waits.\nIt goes: it comes - it stays.\n", "2.00 clause joints per sentence"),
        ("It runs, because it must -- it stops.\nIt waits here.\n", "1.00 clause joints per sentence"),
        (
            "It runs here.\nIt stops there.\n" + PLAIN[:50],
            "66.67 % of sentences outside lists under 8 words (limit 25)",
        ),
    ],
)
def test_each_rate_over_its_limit_fails(
    project: Path, capsys: pytest.CaptureFixture[str], text: str, expected: str
) -> None:
    _configure(project, "min-sentences = 1\n")
    (project / "README.md").write_text(text)
    assert check_prose.main() == 1
    out = capsys.readouterr().out
    assert expected in out
    assert out.endswith("Prose check failed: README.md.\n")


def test_lists_the_worst_sentences_once_per_unit(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    long = "This sentence " + "goes on and on " * 10 + "until it has gone on for far too long to read.\n"
    _configure(project, "min-sentences = 1\n")
    (project / "README.md").write_text(f"# Title\n\nIt runs; it stops.\n{long}")
    assert check_prose.main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "README.md: 2 sentences, over the limits:",
        "- 28.50 words per sentence (limit 18)",
        "  - README.md:4: 53 words: This sentence goes on and on goes on and on goes on and on goes on and on goes…",
        "  - README.md:3: 4 words: It runs; it stops.",
        "- 50.00 % of sentences over 30 words (limit 12)",
        "- 50.00 % of sentences outside lists under 8 words (limit 25)",
        "  - README.md:3: 4 words: It runs; it stops.",
        "- 0.50 semicolons per sentence (limit 0.05)",
        "  - README.md:3: 1 semicolon: It runs; it stops.",
        "Prose check failed: README.md.",
    ]


def test_false_turns_a_limit_off(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _configure(
        project,
        "min-sentences = 1\nmax-short-sentences = false\n"
        "max-semicolons-per-sentence = false\nmax-joints-per-sentence = false\n",
    )
    (project / "README.md").write_text("It runs; it stops.\n")
    assert check_prose.main(show_stats=True) == 0
    out = capsys.readouterr().out
    assert "- 1.00 semicolons per sentence (no limit)\n" in out
    assert "- 4.00 words per sentence (limit 18)\n" in out
    assert "<- over" not in out


def test_short_sentences_are_listed_shortest_first(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = "It reads the file again.\nIt stops there.\nIt waits right here.\n" + PLAIN[:50] * 2
    _configure(project, "min-sentences = 1\n")
    (project / "README.md").write_text(text)
    assert check_prose.main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "README.md: 5 sentences, over the limits:",
        "- 60.00 % of sentences outside lists under 8 words (limit 25)",
        "  - README.md:2: 3 words: It stops there.",
        "  - README.md:3: 4 words: It waits right here.",
        "  - README.md:1: 5 words: It reads the file again.",
        "Prose check failed: README.md.",
    ]
    _configure(project, "min-sentences = 1\nshort-sentence-words = 4\n")
    assert check_prose.main() == 0
    _configure(project, "min-sentences = 1\nshort-sentence-words = 5\nmax-short-sentences = 50\n")
    assert check_prose.main(show_stats=True) == 0
    assert "- 40.00 % of sentences outside lists under 5 words (limit 50)\n" in capsys.readouterr().out


def test_short_sentences_in_list_items_are_not_counted(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _configure(project, "min-sentences = 1\n")
    (project / "README.md").write_text(PLAIN[:50] + "\n- It stops there.\n- It waits right here.\n")
    assert check_prose.main(show_stats=True) == 0
    assert "- 0.00 % of sentences outside lists under 8 words (limit 25)\n" in capsys.readouterr().out
    (project / "README.md").write_text("- It stops there.\n- It waits right here.\n")
    assert check_prose.main(show_stats=True) == 0
    assert "- 0.00 % of sentences outside lists under 8 words (limit 25)\n" in capsys.readouterr().out


def test_short_sentences_that_lead_into_a_list_are_not_counted(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(project, "min-sentences = 1\n")
    (project / "README.md").write_text(PLAIN[:50] + "\nIt does this:\n\n1. It stops there.\n")
    assert check_prose.main(show_stats=True) == 0
    assert "- 0.00 % of sentences outside lists under 8 words (limit 25)\n" in capsys.readouterr().out


def test_settings(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _configure(project, 'files = ["DOC.md", "MISSING.md"]\nmin-sentences = 2\nlong-sentence-words = 3\n')
    (project / "DOC.md").write_text("One two three four.\nOne two three.\n")
    (project / "README.md").write_text("It runs; it stops.\n" * 20)
    assert check_prose.main() == 1
    assert "- 50.00 % of sentences over 3 words (limit 12)\n" in capsys.readouterr().out
    assert check_prose.files() == ["DOC.md", "MISSING.md"]


def test_too_few_sentences_to_judge(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "README.md").write_text("It runs; it stops.\n" * 14)
    (project / "AGENTS.md").write_text("")
    assert check_prose.main() == 0
    assert capsys.readouterr().out == (
        "AGENTS.md: 0 sentences, too few to judge (min-sentences is 15).\n"
        "README.md: 14 sentences, too few to judge (min-sentences is 15).\n"
    )
    _configure(project, "min-sentences = 0\n")
    assert check_prose.main(["AGENTS.md"]) == 0
    assert "AGENTS.md: 0 sentences, too few to judge" in capsys.readouterr().out


def test_stats_show_the_rates_of_a_file_too_short_to_judge(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "README.md").write_text("It runs; it stops.\n")
    (project / "AGENTS.md").write_text("")
    assert check_prose.main(show_stats=True) == 0
    assert capsys.readouterr().out.splitlines() == [
        "AGENTS.md: 0 sentences, too few to judge (min-sentences is 15).",
        "README.md: 1 sentences, too few to judge (min-sentences is 15):",
        "- 4.00 words per sentence (limit 18)",
        "- 0.00 % of sentences over 30 words (limit 12)",
        "- 100.00 % of sentences outside lists under 8 words (limit 25)",
        "- 0.00 commas per sentence (limit 1.3)",
        "- 1.00 semicolons per sentence (limit 0.05)",
        "- 1.00 clause joints per sentence (limit 0.6)",
    ]


def test_no_files(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_prose.main() == 0
    assert capsys.readouterr().out == "No files to check.\n"


def test_run(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "bad.md").write_text("It runs; it stops.\n" * 20)
    (project / "README.md").write_text(PLAIN)
    assert check_prose.run([]) == 0
    assert check_prose.run(["bad.md", "README.md"]) == 1
    assert check_prose.run(["--exit-zero", "--stats", "bad.md"]) == 0
    out = capsys.readouterr().out
    assert "- 1.00 semicolons per sentence (limit 0.05) <- over\n" in out
    assert out.endswith("Prose check failed: bad.md.\n")
    with pytest.raises(SystemExit) as error:
        check_prose.run(["missing.md"])
    assert error.value.code == 2
    assert "missing.md is not a file" in capsys.readouterr().err


def test_reports_a_file_it_cannot_read_and_checks_the_rest(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "AGENTS.md").write_bytes(b"Caf\xe9.\n")
    (project / "README.md").write_text(PLAIN)
    assert check_prose.main() == 1
    assert capsys.readouterr().out.splitlines() == [
        "AGENTS.md: cannot read (not valid UTF-8)",
        "README.md: 20 sentences, within the limits.",
        "Prose check failed: AGENTS.md.",
    ]


def test_exit_zero_does_not_cover_a_file_it_cannot_read(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "bad.md").write_text("It runs; it stops.\n" * 20)
    assert check_prose.main(["bad.md", "gone.md"], exit_zero=True) == 1
    out = capsys.readouterr().out
    assert "gone.md: cannot read (No such file or directory)\n" in out
    assert out.endswith("Prose check failed: bad.md, gone.md.\n")


def test_rejects_bad_settings(project: Path) -> None:
    _configure(project, 'max-commas-per-sentence = "few"\n')
    with pytest.raises(config.ConfigError, match=r"\[tool.repoutils.prose\] max-commas-per-sentence must be a number"):
        check_prose.main()
    _configure(project, "long-sentence-words = 30.9\n")
    with pytest.raises(config.ConfigError, match="long-sentence-words must be a whole number, 0 or more$"):
        check_prose.main()

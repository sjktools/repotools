import io
import re
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import github
from repoutils.commands import labels
from repoutils.commands.labels import Label

CONFIG = """
[[tool.repoutils.labels]]
name = "bug"
color = "#D73A4A"
description = "Something isn't working"

[[tool.repoutils.labels]]
name = "good first issue"

[[tool.repoutils.labels]]
name = "docs"
color = "0075ca"
description = "Documentation"
"""


class FakeGitHub:
    """Answers github.request like the labels API of o/r would, with the given labels."""

    def __init__(self, existing: list[dict[str, Any]]) -> None:
        self.existing = existing
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        assert token == "tok"
        self.calls.append((method, url, json_body))
        if method == "GET":
            prefix = f"{github.API_URL}/repos/o/r/labels?per_page={labels.PER_PAGE}&page="
            assert url.startswith(prefix)
            page = int(url.removeprefix(prefix))
            return self.existing[(page - 1) * labels.PER_PAGE : page * labels.PER_PAGE]
        return json_body

    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]


def _label(name: str, color: str = "ededed", description: str | None = None) -> dict[str, Any]:
    return {"name": name, "color": color, "description": description}


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
    return labels.main([*argv, "--repo", "o/r"])


MATCHING = [
    _label("bug", "d73a4a", "Something isn't working"),
    _label("good first issue", "7057ff", "Good for newcomers"),
    _label("docs", "0075ca", "Documentation"),
    _label("not ours", "ffffff", "Left alone"),
]


@pytest.mark.parametrize("mode", ["check", "ensure"])
@pytest.mark.parametrize("strict", [[], ["--strict"]])
def test_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str, strict: list[str]
) -> None:
    gh = FakeGitHub(MATCHING)
    assert _run(tmp_path, monkeypatch, gh, [mode, *strict]) == 0
    assert gh.writes() == []
    expected = {
        "check": "All 3 label(s) in o/r match pyproject.toml.\n",
        "ensure": "Created or updated 0 of 3 label(s) in o/r.\n",
    }
    assert capsys.readouterr().out == expected[mode]


def test_check_reports_missing_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(MATCHING[:1])
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert gh.writes() == []
    assert capsys.readouterr().out == (
        "good first issue: missing\ndocs: missing\n2 of 3 label(s) in o/r need `repoutils labels ensure`.\n"
    )


def test_check_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub([]), ["check", "--exit-zero"]) == 0
    assert "3 of 3 label(s) in o/r need" in capsys.readouterr().out


def test_check_ignores_unexpected_labels_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(MATCHING), ["check"]) == 0
    assert "not ours" not in capsys.readouterr().out


def test_check_fail_on_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(MATCHING)
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"]) == 1
    assert gh.writes() == []
    assert capsys.readouterr().out == (
        "not ours: not defined in pyproject.toml\n"
        "1 label(s) in o/r aren't defined in pyproject.toml: "
        "add them there (`repoutils labels dump` prints them) or delete them on GitHub.\n"
    )


def test_check_fail_on_unexpected_with_missing_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([MATCHING[0], MATCHING[3]])
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected", "--exit-zero"]) == 0
    assert capsys.readouterr().out == (
        "good first issue: missing\ndocs: missing\nnot ours: not defined in pyproject.toml\n"
        "2 of 3 label(s) in o/r need `repoutils labels ensure`.\n"
        "1 label(s) in o/r aren't defined in pyproject.toml: "
        "add them there (`repoutils labels dump` prints them) or delete them on GitHub.\n"
    )


def test_ensure_creates_missing_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(MATCHING[:1])
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 0
    url = f"{github.API_URL}/repos/o/r/labels"
    assert gh.writes() == [
        ("POST", url, {"name": "good first issue", "color": "ededed"}),
        ("POST", url, {"name": "docs", "color": "0075ca", "description": "Documentation"}),
    ]
    out = capsys.readouterr().out
    assert "good first issue: created\ndocs: created\nCreated or updated 2 of 3 label(s) in o/r." in out


DIFFERENT = [
    # Differs in case, color and a non-empty description: only --strict changes these.
    _label("Bug", "ff0000", "Broken"),
    _label("good first issue"),
    # An empty description is filled in even without --strict.
    _label("docs", "0075ca", ""),
]


def test_check_without_strict_fails_only_on_what_ensure_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(DIFFERENT), ["check"]) == 1
    assert capsys.readouterr().out == (
        "bug: left alone without --strict: name is 'Bug' on GitHub, not 'bug'; "
        "color is 'ff0000' on GitHub, not 'd73a4a'; "
        "description is 'Broken' on GitHub, not \"Something isn't working\"\n"
        "docs: needs updating: description is '' on GitHub, not 'Documentation'\n"
        "1 of 3 label(s) in o/r need `repoutils labels ensure`.\n"
    )


def test_check_without_strict_passes_with_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(DIFFERENT[:2] + MATCHING[2:3]), ["check"]) == 0
    out = capsys.readouterr().out
    assert "bug: left alone without --strict" in out
    assert out.endswith("All 3 label(s) in o/r match pyproject.toml (except as noted).\n")


def test_check_strict_fails_on_any_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(DIFFERENT[:2] + MATCHING[2:3]), ["check", "--strict"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("bug: needs updating: name is 'Bug' on GitHub")
    assert out.endswith("1 of 3 label(s) in o/r need `repoutils labels ensure --strict`.\n")
    assert "left alone" not in out


def test_check_fail_on_unexpected_ignores_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # "Bug" is the label pyproject.toml calls "bug", so it's a difference only --strict counts, not an unexpected label.
    assert (
        _run(tmp_path, monkeypatch, FakeGitHub(DIFFERENT[:2] + MATCHING[2:3]), ["check", "--fail-on-unexpected"]) == 0
    )
    assert capsys.readouterr().out.endswith("All 3 label(s) in o/r match pyproject.toml (except as noted).\n")


def test_ensure_without_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGitHub(DIFFERENT)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 0
    assert gh.writes() == [("PATCH", f"{github.API_URL}/repos/o/r/labels/docs", {"description": "Documentation"})]


def test_ensure_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(DIFFERENT)
    assert _run(tmp_path, monkeypatch, gh, ["ensure", "--strict"]) == 0
    assert gh.writes() == [
        (
            "PATCH",
            f"{github.API_URL}/repos/o/r/labels/Bug",
            {"new_name": "bug", "color": "d73a4a", "description": "Something isn't working"},
        ),
        ("PATCH", f"{github.API_URL}/repos/o/r/labels/docs", {"description": "Documentation"}),
    ]
    assert "bug: updated: name is 'Bug'" in capsys.readouterr().out


def test_ensure_quotes_label_names_in_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGitHub([_label("needs/review ?", "ededed", "")])
    config = '[[tool.repoutils.labels]]\nname = "needs/review ?"\ndescription = "x"\n'
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], config) == 0
    assert gh.writes() == [("PATCH", f"{github.API_URL}/repos/o/r/labels/needs%2Freview%20%3F", {"description": "x"})]


def test_reads_every_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labels, "PER_PAGE", 2)
    gh = FakeGitHub([_label("other")] + MATCHING)
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 0
    assert [url[-1] for method, url, _ in gh.calls] == ["1", "2", "3"]


def test_empty_description_in_config_clears_only_with_strict() -> None:
    label = Label("x", description="")
    existing = _label("x", description="Something")
    assert labels.plan(label, existing, strict=False).fields == {}
    assert labels.plan(label, existing, strict=True).fields == {"description": ""}
    # GitHub returns null for no description, which matches an empty one.
    assert labels.plan(label, _label("x"), strict=True).fields == {}


def test_no_labels_defined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub([])
    assert _run(tmp_path, monkeypatch, gh, ["check"], "[project]\nname = 'x'\n") == 1
    assert gh.calls == []
    assert "No labels are defined" in capsys.readouterr().err


def test_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def request(*args: object, **kwargs: object) -> Any:
        raise urllib.error.HTTPError("https://x/labels", 403, "Forbidden", Message(), io.BytesIO(b"no access"))

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", request)
    # --exit-zero ignores labels needing ensure, but not an error.
    assert labels.main(["check", "--exit-zero", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == "HTTP 403 from https://x/labels:\nno access\n"


def test_gh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def gh_output(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "gh_output", gh_output)
    assert labels.main(["check"]) == 1
    assert capsys.readouterr().err == "gh auth token failed:\nnot logged in\n"


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ("[tool.repoutils]\nlabels = 1\n", "must be an array of tables"),
        ("[[tool.repoutils.labels]]\ncolor = 'ffffff'\n", "needs a non-empty name"),
        ("[[tool.repoutils.labels]]\nname = ' '\n", "needs a non-empty name"),
        ("[[tool.repoutils.labels]]\nname = 'a'\ncolour = 'ffffff'\n", "'a' has unknown setting(s): colour"),
        ("[[tool.repoutils.labels]]\nname = 'a'\n[[tool.repoutils.labels]]\nname = 'A'\n", "'A' is defined twice"),
        ("[[tool.repoutils.labels]]\nname = 'a'\ncolor = 'red'\n", "color must be six hex digits"),
        ("[[tool.repoutils.labels]]\nname = 'a'\ncolor = 0xffffff\n", "color must be six hex digits"),
        ("[[tool.repoutils.labels]]\nname = 'a'\ndescription = 1\n", "description must be a string"),
        (f"[[tool.repoutils.labels]]\nname = 'a'\ndescription = '{'x' * 101}'\n", "at most 100 characters"),
    ],
)
def test_bad_config(tmp_path: Path, config: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config)
    with pytest.raises(ValueError, match=re.escape(error)):
        labels.load_labels(tmp_path)


def test_load_labels(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    assert labels.load_labels(tmp_path) == [
        Label("bug", "d73a4a", "Something isn't working"),
        Label("good first issue"),
        Label("docs", "0075ca", "Documentation"),
    ]


def test_bad_config_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([])
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], "[[tool.repoutils.labels]]\nname = 'a'\ncolor = 'red'\n") == 1
    assert gh.calls == []
    assert "color must be six hex digits" in capsys.readouterr().err


TRICKY = [
    *MATCHING,
    _label("no description", "C2E0C6", ""),
    _label('quote " and \\ backslash', "000000", "Line\nbreak, tab\t, DEL \x7f and é"),
]


def test_dump_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # dump doesn't need pyproject.toml to define any labels.
    assert _run(tmp_path, monkeypatch, FakeGitHub(TRICKY), ["dump"], "") == 0
    out = capsys.readouterr().out
    assert out.startswith('[[tool.repoutils.labels]]\nname = "bug"\ncolor = "d73a4a"\n')
    assert '[[tool.repoutils.labels]]\nname = "no description"\ncolor = "c2e0c6"\n\n' in out
    # Pasted into pyproject.toml, it defines every label exactly as GitHub has it.
    gh = FakeGitHub(TRICKY)
    assert _run(tmp_path, monkeypatch, gh, ["check", "--strict"], out) == 0
    assert capsys.readouterr().out == "All 6 label(s) in o/r match pyproject.toml.\n"
    assert gh.writes() == []


def test_dump_reads_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(labels, "PER_PAGE", 2)
    assert _run(tmp_path, monkeypatch, FakeGitHub(MATCHING), ["dump"], "") == 0
    assert capsys.readouterr().out.count("[[tool.repoutils.labels]]") == 4


def test_dump_no_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # dump neither reads pyproject.toml (this one is malformed) nor changes it.
    config = "[tool.repoutils]\nlabels = 1\n"
    assert _run(tmp_path, monkeypatch, FakeGitHub([]), ["dump"], config) == 0
    assert (tmp_path / "pyproject.toml").read_text() == config
    assert capsys.readouterr() == ("", "o/r has no labels.\n")

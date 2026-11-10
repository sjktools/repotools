from pathlib import Path

import pytest

from repotools.commands.check_attestations import main

WHEEL = "exolot-1.0-py3-none-any.whl"
SDIST = "exolot-1.0.tar.gz"


def write(dist: Path, *names: str, content: str = "x") -> None:
    dist.mkdir(exist_ok=True)
    for name in names:
        (dist / name).write_text(content)


def test_all_attested(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dist = tmp_path / "dist"
    write(dist, WHEEL, SDIST, WHEEL + ".publish.attestation", SDIST + ".publish.attestation")
    assert main([str(dist)]) == 0
    assert "all 2 distributions" in capsys.readouterr().out


def test_missing_and_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dist = tmp_path / "dist"
    write(dist, WHEEL, SDIST, "unrelated.txt")
    write(dist, WHEEL + ".publish.attestation", content="")
    assert main([str(dist)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"::error::Empty attestation for {(dist / WHEEL).as_posix()}",
        f"::error::No attestation for {(dist / SDIST).as_posix()}",
    ]


def test_attestation_is_a_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dist = tmp_path / "dist"
    write(dist, WHEEL)
    (dist / (WHEEL + ".publish.attestation")).mkdir()
    assert main([str(dist)]) == 1
    assert "No attestation for" in capsys.readouterr().out


@pytest.mark.parametrize("create", [True, False])
def test_no_distributions(tmp_path: Path, capsys: pytest.CaptureFixture[str], create: bool) -> None:
    dist = tmp_path / "dist"
    if create:
        write(dist, "README")
    assert main([str(dist)]) == 1
    assert capsys.readouterr().out == f"::error::No distributions found in {dist.as_posix()}/\n"


def test_default_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "dist", WHEEL, WHEEL + ".publish.attestation")
    monkeypatch.setattr("sys.argv", ["check_attestations.py"])
    assert main() == 0
    assert "in dist/" in capsys.readouterr().out


def test_too_many_args(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["a", "b"]) == 2
    assert "usage" in capsys.readouterr().err

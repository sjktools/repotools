import io
import re
import shutil
import subprocess
import tomllib
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import github
from repoutils.commands import deploy_keys
from repoutils.commands.deploy_keys import DeployKey, Secret

CONFIG = """
[tool.repoutils.deploy-keys.release]
write = true
secret = { environment = "release", name = "RELEASE_DEPLOY_KEY" }

[tool.repoutils.deploy-keys.docs]
secret = { name = "DOCS_KEY" }

[tool.repoutils.deploy-keys."read only"]
"""

API = f"{github.API_URL}/repos/o/r"
OLD, NEW = "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"


def _key(ident: int, title: str, *, write: bool = False, created: str = OLD) -> dict[str, Any]:
    """A deploy key as GitHub returns it."""
    return {
        "id": ident,
        "title": title,
        "key": f"ssh-ed25519 KEY{ident}",
        "read_only": not write,
        "created_at": created,
    }


class FakeGitHub:
    """Answers github.request like the deploy keys, secrets and environments APIs of o/r would."""

    def __init__(
        self,
        keys: list[dict[str, Any]],
        secrets: dict[tuple[str | None, str], str] | None = None,
        environments: tuple[str, ...] = ("release",),
        *,
        fail_add: bool = False,
    ) -> None:
        self.keys = list(keys)
        # When each secret was set, by its environment (None for the repository's) and name.
        self.secrets = dict(secrets or {})
        self.environments = environments
        self.fail_add = fail_add
        self.fail_delete = False
        self.next_id = 100
        self.calls: list[tuple[str, str, Any]] = []

    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        assert token == "tok"
        self.calls.append((method, url, json_body))
        path, _, query = url.partition("?")
        assert path.startswith(API)
        parts = [urllib.parse.unquote(part) for part in path.removeprefix(API).split("/")[1:]]
        if parts == ["keys"]:
            if method == "GET":
                page = int(urllib.parse.parse_qs(query)["page"][0])
                return self.keys[(page - 1) * deploy_keys.PER_PAGE : page * deploy_keys.PER_PAGE]
            assert method == "POST"
            if self.fail_add:
                raise urllib.error.HTTPError(url, 422, "Unprocessable", Message(), io.BytesIO(b"key is in use"))
            self.next_id += 1
            added = {
                "id": self.next_id,
                "title": json_body["title"],
                "key": json_body["key"],
                "read_only": json_body["read_only"],
                "created_at": NEW,
            }
            self.keys.append(added)
            return added
        if parts[0] == "keys":
            assert method == "DELETE"
            if self.fail_delete:
                raise urllib.error.HTTPError(url, 500, "Error", Message(), io.BytesIO(b""))
            self.keys = [key for key in self.keys if key["id"] != int(parts[1])]
            return None
        if parts == ["environments"]:
            names = [{"name": name} for name in self.environments]
            return {"total_count": len(names), "environments": names}
        environment = None if parts[0] == "actions" else parts[1]
        assert method == "GET"
        updated = self.secrets.get((environment, parts[-1]))
        if updated is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b""))
        return {"name": parts[-1], "created_at": OLD, "updated_at": updated}


class FakeTools:
    """Stands in for ssh-keygen and gh, setting secrets on gh, a FakeGitHub."""

    def __init__(self, gh: FakeGitHub, *, fail_gh: bool = False) -> None:
        self.gh = gh
        self.fail_gh = fail_gh
        self.generated = 0
        self.commands: list[list[str]] = []
        self.secret_values: list[bytes] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        self.commands.append(command)
        if command[0] == "ssh-keygen":
            self.generated += 1
            path = Path(command[command.index("-f") + 1])
            # The temporary directory is private to us.
            assert path.parent.stat().st_mode & 0o077 == 0
            path.write_text(f"PRIVATE{self.generated}\n")
            path.with_suffix(".pub").write_text(f"ssh-ed25519 NEW{self.generated} {command[command.index('-C') + 1]}\n")
            return subprocess.CompletedProcess(command, 0, "", "")
        assert command[:3] == ["gh", "secret", "set"]
        assert kwargs["env"]["GH_TOKEN"] == "tok"
        if self.fail_gh:
            return subprocess.CompletedProcess(command, 1, b"", b"HTTP 403")
        self.secret_values.append(kwargs["input"])
        environment = command[command.index("--env") + 1] if "--env" in command else None
        self.gh.secrets[(environment, command[3])] = "2026-03-01T00:00:00Z"
        return subprocess.CompletedProcess(command, 0, b"", b"")


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gh: FakeGitHub,
    argv: list[str],
    config: str = CONFIG,
    tools: FakeTools | None = None,
) -> int:
    (tmp_path / "pyproject.toml").write_text(config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(subprocess, "run", tools or FakeTools(gh))
    return deploy_keys.main([*argv, "--repo", "o/r"])


def _matching() -> FakeGitHub:
    return FakeGitHub(
        [_key(1, "release", write=True), _key(2, "docs"), _key(3, "read only")],
        {("release", "RELEASE_DEPLOY_KEY"): NEW, (None, "DOCS_KEY"): OLD},
    )


def test_load(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    assert deploy_keys.load_deploy_keys(tmp_path) == [
        DeployKey("release", True, Secret("RELEASE_DEPLOY_KEY", "release")),
        DeployKey("docs", False, Secret("DOCS_KEY")),
        DeployKey("read only"),
    ]
    # No table declares nothing, but an empty one declares that there are no keys.
    (tmp_path / "pyproject.toml").write_text("[tool.repoutils]\n")
    assert deploy_keys.load_deploy_keys(tmp_path) is None
    (tmp_path / "pyproject.toml").write_text("[tool.repoutils.deploy-keys]\n")
    assert deploy_keys.load_deploy_keys(tmp_path) == []


T = "[tool.repoutils.deploy-keys.k]\n"


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ("[tool.repoutils]\ndeploy-keys = 1\n", "[tool.repoutils.deploy-keys] must be a table"),
        ("[tool.repoutils.deploy-keys]\nk = 1\n", "[tool.repoutils.deploy-keys.k] must be a table"),
        (T + "read-only = true\n", "unknown setting(s): read-only"),
        (T + "write = 'yes'\n", "write must be true or false"),
        (T + "secret = 'X'\n", "secret must be a table"),
        (T + "secret = { name = 'X', env = 'e' }\n", "secret has unknown setting(s): env"),
        (T + "secret = { environment = 'e' }\n", "secret needs a name"),
        (T + "secret = { name = 'X', environment = '' }\n", "environment must be a non-empty string"),
        (
            T + "secret = { name = 'S', environment = 'E' }\n[tool.repoutils.deploy-keys.j]\n"
            "secret = { name = 's', environment = 'e' }\n",
            "[tool.repoutils.deploy-keys.j] secret is also [tool.repoutils.deploy-keys.k]'s",
        ),
        (
            T + "secret = { name = 'S' }\n[tool.repoutils.deploy-keys.j]\nsecret = { name = 'S' }\n",
            "[tool.repoutils.deploy-keys.j] secret is also",
        ),
    ],
)
def test_bad_config(tmp_path: Path, config: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config)
    with pytest.raises(ValueError, match=re.escape(error)):
        deploy_keys.load_deploy_keys(tmp_path)


@pytest.mark.parametrize("mode", ["check", "ensure"])
def test_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    gh = _matching()
    # A read-only key nobody declares is fine without --fail-on-unexpected.
    gh.keys.append(_key(4, "someone's"))
    assert _run(tmp_path, monkeypatch, gh, [mode]) == 0
    assert gh.writes() == []
    assert "3 deploy key(s) in o/r" in capsys.readouterr().out


def test_check_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(
        [_key(1, "release"), _key(2, "docs", created=NEW), _key(5, "extra", write=True), _key(6, "ro")],
        {("release", "RELEASE_DEPLOY_KEY"): NEW, (None, "DOCS_KEY"): OLD},
    )
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"]) == 1
    assert capsys.readouterr().out == (
        "release: read-only on GitHub, not write\n"
        "docs: secret DOCS_KEY was last set before the key was added, so it can't hold its private half\n"
        "read only: missing\n"
        "extra: write deploy key not declared in pyproject.toml\n"
        "ro: read-only deploy key not declared in pyproject.toml\n"
        "3 of 3 deploy key(s) in o/r need `repoutils deploy-keys ensure`.\n"
        "2 deploy key(s) in o/r aren't declared in pyproject.toml: "
        "declare them there (`repoutils deploy-keys dump` prints them) or delete them on GitHub.\n"
    )
    # --exit-zero forgives them.
    assert _run(tmp_path, monkeypatch, gh, ["check", "--exit-zero"]) == 0


def test_check_secret_and_environment_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_key(1, "release", write=True), _key(2, "docs"), _key(3, "x"), _key(4, "x")], environments=())
    config = CONFIG.replace('[tool.repoutils.deploy-keys."read only"]', "[tool.repoutils.deploy-keys.x]")
    assert _run(tmp_path, monkeypatch, gh, ["check"], config) == 1
    assert capsys.readouterr().out.splitlines()[:3] == [
        "release: environment release, which holds its secret RELEASE_DEPLOY_KEY, is missing",
        "docs: secret DOCS_KEY is missing",
        "x: 2 deploy keys have this title (ids 3, 4, oldest first): delete all but one on GitHub",
    ]
    # A missing key says its environment is missing too, which stops ensure creating it.
    assert _run(tmp_path, monkeypatch, FakeGitHub([], environments=()), ["check"]) == 1
    assert capsys.readouterr().out.splitlines()[:3] == [
        "release: missing",
        "release: environment release, which holds its secret RELEASE_DEPLOY_KEY, is missing",
        "docs: missing",
    ]


def test_empty_table_forbids_write_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_key(1, "ro")])
    assert _run(tmp_path, monkeypatch, gh, ["check"], "[tool.repoutils.deploy-keys]\n") == 0
    assert "All 0 deploy key(s)" in capsys.readouterr().out
    gh.keys.append(_key(2, "rw", write=True))
    assert _run(tmp_path, monkeypatch, gh, ["check"], "[tool.repoutils.deploy-keys]\n") == 1


def test_ensure_creates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub([_key(3, "read only")])
    tools = FakeTools(gh)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], tools=tools) == 0
    assert capsys.readouterr().out == (
        "release: created, and environment release secret RELEASE_DEPLOY_KEY set\n"
        "docs: created, and secret DOCS_KEY set\n"
        "Created or changed 2 of 3 deploy key(s) in o/r.\n"
    )
    assert [(key["title"], key["key"], key["read_only"]) for key in gh.keys] == [
        ("read only", "ssh-ed25519 KEY3", True),
        ("release", "ssh-ed25519 NEW1 release", False),
        ("docs", "ssh-ed25519 NEW2 docs", True),
    ]
    assert tools.secret_values == [b"PRIVATE1\n", b"PRIVATE2\n"]
    assert tools.commands[1] == [
        "gh",
        "secret",
        "set",
        "RELEASE_DEPLOY_KEY",
        "--repo",
        "github.com/o/r",
        "--env",
        "release",
    ]
    assert tools.commands[3] == ["gh", "secret", "set", "DOCS_KEY", "--repo", "github.com/o/r"]
    # The temporary directories are gone.
    assert not any(Path(command[command.index("-f") + 1]).parent.exists() for command in tools.commands[::2])


def test_ensure_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = _matching()
    assert _run(tmp_path, monkeypatch, gh, ["ensure", "--rotate", "release"]) == 0
    assert (
        "release: replaced with a new key pair, and environment release secret RELEASE_DEPLOY_KEY set\n"
        in capsys.readouterr().out
    )
    release = [key for key in gh.keys if key["title"] == "release"]
    assert [(key["key"], key["read_only"]) for key in release] == [("ssh-ed25519 NEW1 release", False)]
    # The new key is added before the old one is deleted.
    assert [call[0] for call in gh.writes()] == ["POST", "DELETE"]


def test_ensure_fixes_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = _matching()
    gh.keys[0] = _key(1, "release", write=False)
    gh.keys[2] = _key(3, "read only", write=True)
    # A missing secret doesn't stop ensure replacing a key it's replacing anyway.
    del gh.secrets[("release", "RELEASE_DEPLOY_KEY")]
    tools = FakeTools(gh)
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], tools=tools) == 0
    assert capsys.readouterr().out == (
        # With a secret, it gets a new key pair.
        "release: replaced with a new key pair, and environment release secret RELEASE_DEPLOY_KEY set\n"
        # Without one, the same public key is added again.
        "read only: given read-only access\n"
        "Created or changed 2 of 3 deploy key(s) in o/r.\n"
    )
    assert [command[0] for command in tools.commands] == ["ssh-keygen", "gh"]
    assert gh.keys == [
        _key(2, "docs"),
        {**_key(1, "release", write=True), "id": 101, "key": "ssh-ed25519 NEW1 release", "created_at": NEW},
        {**_key(3, "read only"), "id": 102, "created_at": NEW},
    ]


def test_ensure_fix_access_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = _matching()
    gh.keys[1] = _key(2, "docs", write=True)
    gh.keys[2] = _key(3, "read only", write=True)
    gh.fail_add = True
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], CONFIG.replace('secret = { name = "DOCS_KEY" }', "")) == 1
    out, err = capsys.readouterr()
    assert "docs: deleted, but adding it again failed; its public key is ssh-ed25519 KEY2\n" in out
    assert "HTTP 422" in err


def test_ensure_removes_key_when_secret_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = _matching()
    old = list(gh.keys)
    tools = FakeTools(gh, fail_gh=True)
    assert _run(tmp_path, monkeypatch, gh, ["ensure", "--rotate", "release"], tools=tools) == 1
    # The new key is deleted, and the old one, whose secret is unchanged, kept.
    assert gh.keys == old
    assert [call[0] for call in gh.writes()] == ["POST", "DELETE"]
    assert capsys.readouterr().err == (
        "gh secret set RELEASE_DEPLOY_KEY --repo github.com/o/r --env release failed:\nHTTP 403\n"
    )


@pytest.mark.parametrize(
    ("gh", "argv", "config", "error"),
    [
        (FakeGitHub([]), ["--rotate", "nope"], CONFIG, "--rotate names deploy key(s) pyproject.toml doesn't declare"),
        (FakeGitHub([]), [], T, "k: no secret is declared to hold the private half of a new key"),
        (FakeGitHub([_key(1, "k")]), ["--rotate", "k"], T, "k: no secret is declared"),
        (FakeGitHub([_key(1, "k"), _key(2, "k")]), [], T, "k: 2 deploy keys have this title"),
        (
            FakeGitHub([], environments=()),
            [],
            T + "secret = { name = 'S', environment = 'e' }\n",
            "k: environment e, which holds its secret S, is missing: create it with `repoutils environments ensure`",
        ),
        (
            FakeGitHub([_key(1, "k")]),
            [],
            T + "secret = { name = 'S' }\n",
            "k: secret S is missing: replace the key with `--rotate k`",
        ),
        (
            # Quoted for a shell, to paste.
            FakeGitHub([_key(1, "$(id) x")]),
            [],
            "[tool.repoutils.deploy-keys.'$(id) x']\nsecret = { name = 'S' }\n",
            "replace the key with `--rotate '$(id) x'`",
        ),
    ],
)
def test_ensure_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gh: FakeGitHub,
    argv: list[str],
    config: str,
    error: str,
) -> None:
    assert _run(tmp_path, monkeypatch, gh, ["ensure", *argv], config) == 1
    assert gh.writes() == []
    assert error in capsys.readouterr().err


def test_ensure_needs_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_key(3, "read only")])
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    monkeypatch.setattr(shutil, "which", lambda tool: None)
    assert deploy_keys.main(["ensure", "--repo", "o/r"]) == 1
    assert gh.writes() == []
    assert capsys.readouterr().err == "Creating a deploy key needs ssh-keygen and gh on PATH.\n"


def test_ensure_reports_what_github_did_not_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_key(3, "read only"), _key(9, "stray", write=True)])
    tools = FakeTools(gh)
    # gh "succeeds" without setting the secret.
    tools.gh = FakeGitHub([])
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], tools=tools) == 1
    out, err = capsys.readouterr()
    assert "stray: write deploy key not declared in pyproject.toml (ensure leaves it alone)\n" in out
    assert "After ensure, docs: secret DOCS_KEY is missing\n" in err
    assert any(key["title"] == "stray" for key in gh.keys)


def test_dump_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub([_key(1, "release", write=True), _key(2, "a b"), _key(3, "a b", write=True)])
    # dump neither reads pyproject.toml (this one is malformed) nor changes it.
    config = "[tool.repoutils]\ndeploy-keys = 1\n"
    assert _run(tmp_path, monkeypatch, gh, ["dump"], config) == 0
    assert (tmp_path / "pyproject.toml").read_text() == config
    out, err = capsys.readouterr()
    tomllib.loads(out)
    assert err == "2 deploy keys are titled 'a b', so it's printed once.\n"
    assert (
        out
        == '[tool.repoutils.deploy-keys.release]\nwrite = true\n\n[tool.repoutils.deploy-keys."a b"]\nwrite = true\n'
    )
    gh.keys.pop()
    assert _run(tmp_path, monkeypatch, gh, ["dump"], config) == 0
    out = capsys.readouterr().out
    tomllib.loads(out)
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], out) == 0
    assert gh.writes() == []


def test_dump_no_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub([]), ["dump"], "") == 0
    assert capsys.readouterr() == ("", "o/r has no deploy keys.\n")


def test_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGitHub([_key(i, f"k{i}") for i in range(deploy_keys.PER_PAGE)] + [_key(999, "last", write=True)])
    assert _run(tmp_path, monkeypatch, gh, ["check"], "[tool.repoutils.deploy-keys.last]\nwrite = true\n") == 0


def test_none_declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub([])
    assert _run(tmp_path, monkeypatch, gh, ["check"], "[project]\nname = 'x'\n") == 1
    assert gh.calls == []
    assert "No deploy keys are declared" in capsys.readouterr().err


def test_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def request(*args: object, **kwargs: object) -> Any:
        raise urllib.error.HTTPError("https://x/keys", 403, "Forbidden", Message(), io.BytesIO(b"nope"))

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", request)
    # --exit-zero forgives keys needing ensure, but not an error.
    assert deploy_keys.main(["check", "--exit-zero", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == "HTTP 403 from https://x/keys:\nnope\n"


def test_secret_lookup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = _matching()

    def request(method: str, url: str, token: str, **kwargs: Any) -> Any:
        if "/secrets/" in url:
            raise urllib.error.HTTPError(url, 500, "Error", Message(), io.BytesIO(b"oops"))
        return gh(method, url, token, **kwargs)

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", request)
    assert deploy_keys.main(["check", "--exit-zero", "--repo", "o/r"]) == 1
    assert "HTTP 500" in capsys.readouterr().err


def test_generate_runs_ssh_keygen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGitHub([])
    tools = FakeTools(gh)
    monkeypatch.setattr(subprocess, "run", tools)
    assert deploy_keys.generate("t", tmp_path) == ("ssh-ed25519 NEW1 t", b"PRIVATE1\n")
    assert tools.commands == [["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(tmp_path / "key")]]


def test_bad_config_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([])
    assert _run(tmp_path, monkeypatch, gh, ["check", "--exit-zero"], T + "write = 1\n") == 1
    assert gh.calls == []
    assert capsys.readouterr().err == "[tool.repoutils.deploy-keys.k] write must be true or false\n"


def test_same_secret_name_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A repository secret and environments' secrets of the same name are different secrets.
    config = (
        "[tool.repoutils.deploy-keys.a]\nsecret = { name = 'S' }\n"
        "[tool.repoutils.deploy-keys.b]\nsecret = { name = 'S', environment = 'release' }\n"
        "[tool.repoutils.deploy-keys.c]\nsecret = { name = 'S', environment = 'docs' }\n"
    )
    gh = FakeGitHub(
        [_key(1, "a"), _key(2, "b"), _key(3, "c")],
        {(None, "S"): OLD, ("release", "S"): OLD, ("docs", "S"): OLD},
        ("release", "docs"),
    )
    assert _run(tmp_path, monkeypatch, gh, ["check"], config) == 0
    # The environments are listed once, for both keys.
    assert sum(url.startswith(f"{API}/environments?") for _, url, _ in gh.calls) == 1


def test_duplicates_after_failed_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The old key couldn't be deleted after the secret was set to the new key's private half.
    gh = FakeGitHub([_key(9, "docs", created=NEW), _key(2, "docs")], {(None, "DOCS_KEY"): "2026-03-01T00:00:00Z"})
    config = '[tool.repoutils.deploy-keys.docs]\nsecret = { name = "DOCS_KEY" }\n'
    assert _run(tmp_path, monkeypatch, gh, ["check"], config) == 1
    assert capsys.readouterr().out.splitlines()[0] == (
        "docs: 2 deploy keys have this title: its secret was set after the newest (id 9), so if it holds that "
        "key's private half, delete the others on GitHub (id 2); if unsure, delete them all and run ensure"
    )


@pytest.mark.parametrize("delete_fails", [False, True])
def test_interrupted_secret_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], delete_fails: bool
) -> None:
    gh = _matching()
    tools = FakeTools(gh)

    def interrupted(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if command[0] == "gh":
            if delete_fails:
                gh.fail_delete = True
            raise KeyboardInterrupt
        return tools(command, **kwargs)

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(subprocess, "run", interrupted)
    # The interruption, not a failed clean-up, is what's raised.
    with pytest.raises(KeyboardInterrupt):
        deploy_keys.main(["ensure", "--rotate", "release", "--repo", "o/r"])
    err = capsys.readouterr().err
    assert "release: interrupted while setting environment release secret RELEASE_DEPLOY_KEY" in err
    assert "`repoutils deploy-keys ensure --rotate release`" in err
    assert ("couldn't delete the key just added (id 101)" in err) == delete_fails
    assert ("once key id 101 is deleted, run" in err) == delete_fails
    assert [key["id"] for key in gh.keys] == ([1, 2, 3, 101] if delete_fails else [1, 2, 3])

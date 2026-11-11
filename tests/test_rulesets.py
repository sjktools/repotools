import copy
import io
import json
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import github
from repoutils.commands import rulesets

API = github.API_URL
CONFIG = """
[tool.repoutils.rulesets.main]
include = ["~DEFAULT_BRANCH"]
bypass = [{ role = "admin", mode = "pull-request" }, { team = "core" }]

[tool.repoutils.rulesets.main.rules]
deletion = true

[tool.repoutils.rulesets.main.rules.required-status-checks]
required-status-checks = [{ context = "build", app = "github-actions" }]
"""
# What GitHub has for a ruleset that matches CONFIG's.
MATCHING: dict[str, Any] = {
    "id": 1,
    "name": "main",
    "target": "branch",
    "source_type": "Repository",
    "source": "o/r",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "bypass_actors": [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"},
        {"actor_id": 42, "actor_type": "Team", "bypass_mode": "always"},
    ],
    "rules": [
        {"type": "deletion"},
        {
            "type": "required_status_checks",
            "parameters": {
                "do_not_enforce_on_create": False,
                "required_status_checks": [{"context": "build", "integration_id": 15368}],
                "strict_required_status_checks_policy": False,
            },
        },
    ],
}
TAG_RULESET = {"id": 2, "name": "tags", "target": "tag", "source_type": "Repository", "rules": []}
OTHER = MATCHING | {"id": 3, "name": "other", "bypass_actors": []}


def _not_found(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b"{}"))


class FakeGitHub:
    """Answers github.request like GitHub's API would for o/r, applying each write
    (except the rules in ignored)."""

    def __init__(self, *existing: dict[str, Any], ignored: frozenset[str] = frozenset()) -> None:
        self.rulesets = {r["id"]: copy.deepcopy(r) for r in existing}
        self.ignored = ignored
        self.writes: list[tuple[str, str, Any]] = []
        self.reads: list[str] = []
        self.apps = {"github-actions": 15368, "dependabot": 29110}
        self.check_apps = [{"id": 15368, "slug": "github-actions"}]

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        assert token == "tok"
        path = url.removeprefix(API)
        self.reads.append(path)
        if method != "GET":
            self.writes.append((method, path, json_body))
            stored = {k: v for k, v in json_body.items()} | {"source_type": "Repository"}
            stored["rules"] = [r for r in stored["rules"] if r["type"] not in self.ignored]
            if method == "POST":
                stored["id"] = max(self.rulesets, default=0) + 10
            else:
                stored["id"] = int(path.rsplit("/", 1)[1])
            self.rulesets[stored["id"]] = stored
            return copy.deepcopy(stored)
        if path.startswith("/repos/o/r/rulesets?"):
            assert "includes_parents=false" in path
            return [{k: r[k] for k in ("id", "name", "target", "source_type")} for r in self.rulesets.values()]
        if path.startswith("/repos/o/r/rulesets/"):
            return copy.deepcopy(self.rulesets[int(path.rsplit("/", 1)[1])])
        if path.startswith("/apps/"):
            slug = path.removeprefix("/apps/")
            if slug not in self.apps:
                raise _not_found(url)
            return {"id": self.apps[slug], "slug": slug}
        if path == "/orgs/o/teams/core":
            return {"id": 42, "slug": "core"}
        if path.startswith("/orgs/o/teams?"):
            return [{"id": 42, "slug": "core"}]
        if path.startswith("/orgs/o/teams/"):
            raise _not_found(url)
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if path.startswith("/repos/o/r/commits/main/check-runs"):
            page = int(path.rsplit("page=", 1)[1])
            return {"check_runs": [{"app": app} for app in self.check_apps[(page - 1) * 100 : page * 100]]}
        if path.startswith("/repos/o/r/commits/main/check-suites"):
            return {"check_suites": [{"app": None}]}
        raise _not_found(url)


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gh: FakeGitHub, argv: list[str], config: str = CONFIG) -> int:
    (tmp_path / "pyproject.toml").write_text(config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", gh)
    return rulesets.main([*argv, "--repo", "o/r"])


@pytest.mark.parametrize("mode", ["check", "ensure"])
def test_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    gh = FakeGitHub(MATCHING, TAG_RULESET)
    assert _run(tmp_path, monkeypatch, gh, [mode]) == 0
    assert gh.writes == []
    expected = {
        "check": "All 1 ruleset(s) in o/r match pyproject.toml.\n",
        "ensure": "Created or updated 0 of 1 ruleset(s) in o/r.\n",
    }
    assert capsys.readouterr().out == expected[mode]


def test_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(TAG_RULESET)
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert capsys.readouterr().out == "main: missing\n1 of 1 ruleset(s) in o/r need `repoutils rulesets ensure`.\n"
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 0
    assert capsys.readouterr().out == "main: created\nCreated or updated 1 of 1 ruleset(s) in o/r.\n"
    [(method, path, body)] = gh.writes
    assert (method, path) == ("POST", "/repos/o/r/rulesets")
    assert body == {k: MATCHING[k] for k in ("name", "target", "enforcement", "conditions", "bypass_actors", "rules")}
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 0


DIFFERENT = MATCHING | {"enforcement": "disabled", "rules": [{"type": "non_fast_forward"}]}


def test_different(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(DIFFERENT)
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert capsys.readouterr().out == (
        'main: needs updating: enforcement is "disabled" on GitHub, not "active"; rule deletion is off on GitHub; '
        "rule required-status-checks is off on GitHub; "
        "rule non-fast-forward is on on GitHub, but pyproject.toml leaves it off\n"
        "1 of 1 ruleset(s) in o/r need `repoutils rulesets ensure`.\n"
    )
    assert _run(tmp_path, monkeypatch, gh, ["check", "--exit-zero"]) == 0
    capsys.readouterr()
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 0
    assert capsys.readouterr().out.startswith("main: updated: enforcement is")
    assert [(method, path) for method, path, _ in gh.writes] == [("PUT", "/repos/o/r/rulesets/1")]
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 0


def test_ensure_fails_if_github_ignores_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(DIFFERENT, ignored=frozenset({"deletion"}))
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 1
    assert capsys.readouterr().err == "GitHub didn't apply the change: main: rule deletion is off on GitHub\n"


def test_fail_on_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(MATCHING, OTHER, TAG_RULESET)
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 0
    capsys.readouterr()
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"]) == 1
    assert capsys.readouterr().out == (
        "other: not defined in pyproject.toml\n"
        "1 branch ruleset(s) in o/r aren't defined in pyproject.toml: "
        "add them there (`repoutils rulesets dump` prints them) or delete them on GitHub.\n"
    )


@pytest.mark.parametrize(
    ("bypass", "error"),
    [
        ('{ app = "nope" }', "There's no GitHub App with the slug 'nope'\n"),
        ('{ team = "nope" }', "o has no team with the slug 'nope'\n"),
    ],
)
def test_unknown_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bypass: str, error: str
) -> None:
    config = f"[tool.repoutils.rulesets.main]\ninclude = ['main']\nbypass = [{bypass}]\n"
    assert _run(tmp_path, monkeypatch, FakeGitHub(), ["check"], config) == 1
    assert capsys.readouterr().err == error


def test_each_slug_is_looked_up_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGitHub()
    config = """
[tool.repoutils.rulesets.main]
include = ["main"]
bypass = [
    { app = "dependabot" },
    { app = "dependabot", mode = "exempt" },
    { team = "core" },
    { team = "core", mode = "exempt" },
]
"""
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], config) == 0
    assert [a["actor_id"] for a in gh.writes[0][2]["bypass_actors"]] == [29110, 29110, 42, 42]
    assert gh.reads.count("/apps/dependabot") == 1
    assert gh.reads.count("/orgs/o/teams/core") == 1


def test_dump_from_a_malformed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "x.json").write_text("[1]")
    assert _run(tmp_path, monkeypatch, FakeGitHub(), ["dump", "--from-file", str(tmp_path / "x.json")], "") == 1
    assert capsys.readouterr().err == f"{tmp_path / 'x.json'} must hold a ruleset JSON object, or a list of them\n"


def test_other_http_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()

    def request(method: str, url: str, token: str, **kwargs: Any) -> Any:
        if "/apps/" in url:
            raise urllib.error.HTTPError(url, 500, "Oops", Message(), io.BytesIO(b"boom"))
        return gh(method, url, token, **kwargs)

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", request)
    assert rulesets.main(["check", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == f"HTTP 500 from {API}/apps/github-actions:\nboom\n"


def test_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(MATCHING, TAG_RULESET)
    assert _run(tmp_path, monkeypatch, gh, ["dump"], "") == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out.startswith('[tool.repoutils.rulesets.main]\ninclude = ["~DEFAULT_BRANCH"]\n')
    # Pasted into pyproject.toml, check passes.
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], out) == 0


def test_dump_reads_every_page_of_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(MATCHING)
    gh.check_apps = [{"id": i, "slug": f"app{i}"} for i in range(100)] + gh.check_apps
    gh.apps.pop("github-actions")
    assert _run(tmp_path, monkeypatch, gh, ["dump"], "") == 0
    assert '{ context = "build", app = "github-actions" }' in capsys.readouterr().out


def test_dump_finds_a_known_app_or_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(MATCHING | {"bypass_actors": [{"actor_id": 29110, "actor_type": "Integration"}]})
    gh.check_apps = []
    gh.apps.pop("github-actions")
    assert _run(tmp_path, monkeypatch, gh, ["dump"], "") == 0
    out, err = capsys.readouterr()
    assert '{ app = "dependabot" }' in out
    assert '{ context = "build", integration-id = 15368 }' in out
    assert err == (
        "main: couldn't find the slug of app 15368, so gave its ID, which check refuses: "
        "replace it with app = <its slug>\n"
    )


def test_dump_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(TAG_RULESET), ["dump"], "") == 0
    assert capsys.readouterr() == ("", "o/r has no branch rulesets.\n")


def test_dump_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    export = MATCHING | {"name": "exported", "conditions": {"ref_name": {"include": ["refs/heads/dev"]}}}
    (tmp_path / "one.json").write_text(json.dumps(export))
    (tmp_path / "two.json").write_text(json.dumps([TAG_RULESET]))
    argv = ["dump", "--from-file", str(tmp_path / "one.json"), "--from-file", str(tmp_path / "two.json")]
    assert _run(tmp_path, monkeypatch, FakeGitHub(), argv, "") == 0
    out, err = capsys.readouterr()
    assert out.startswith('[tool.repoutils.rulesets.exported]\ninclude = ["dev"]\n')
    assert '{ context = "build", app = "github-actions" }' in out
    assert err == "Left out 'tags': it's a tag ruleset, not a branch one\n"


def test_no_rulesets_defined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(), ["check"], "") == 1
    assert "No rulesets are defined" in capsys.readouterr().err


def test_malformed_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub(), ["check"], "[tool.repoutils.rulesets]\nmain = 1\n") == 1
    assert capsys.readouterr().err == "[tool.repoutils.rulesets.main] must be a table\n"


def test_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {1: [{"id": i, "name": f"r{i}", "target": "branch"} for i in range(100)], 2: [TAG_RULESET, OTHER]}

    def request(method: str, url: str, token: str, **kwargs: Any) -> Any:
        return pages[int(url.rsplit("page=", 1)[1])]

    monkeypatch.setattr(github, "request", request)
    found = rulesets.repo_rulesets("o/r", "tok")
    assert len(found) == 101
    assert found[-1]["name"] == "other"


def test_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def fail(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    monkeypatch.setattr(github, "gh_output", fail)
    assert rulesets.main(["check", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == "gh auth token failed:\nnot logged in\n"

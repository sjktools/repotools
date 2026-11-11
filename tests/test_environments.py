import io
import re
import subprocess
import urllib.error
import urllib.parse
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from repoutils import github
from repoutils.commands import environments
from repoutils.commands.environments import Environment

CONFIG = """
[tool.repoutils.environments.pypi]
allowed-tags = ["v*"]
allowed-branches = []
reviewers = ["alice", "o/devs"]
prevent-self-review = true

[tool.repoutils.environments.testpypi]
deployment-branches = "all"
wait-timer = 5

[tool.repoutils.environments.docs]
"""

API = f"{github.API_URL}/repos/o/r/environments"
USERS = {"alice": 1, "bob": 2}
TEAMS = {"devs": 10}


def _reviewer(kind: str, ident: int) -> dict[str, Any]:
    if kind == "Team":
        slug = next(slug for slug, i in TEAMS.items() if i == ident)
        return {"type": "Team", "reviewer": {"id": ident, "slug": slug, "name": slug.title()}}
    login = next(login for login, i in USERS.items() if i == ident)
    return {"type": "User", "reviewer": {"id": ident, "login": login}}


def _env(
    name: str,
    *,
    deployment: str = "all",
    wait: int = 0,
    reviewers: list[tuple[str, int]] | None = None,
    prevent: bool = False,
    bypass: bool = True,
) -> dict[str, Any]:
    """An environment as GitHub returns it."""
    rules: list[dict[str, Any]] = []
    if wait:
        rules.append({"id": 1, "type": "wait_timer", "wait_timer": wait})
    if reviewers:
        reviewer_list = [_reviewer(kind, ident) for kind, ident in reviewers]
        rules.append(
            {"id": 2, "type": "required_reviewers", "prevent_self_review": prevent, "reviewers": reviewer_list}
        )
    policy = None
    if deployment != "all":
        policy = {"protected_branches": deployment == "protected", "custom_branch_policies": deployment == "selected"}
        rules.append({"id": 3, "type": "branch_policy"})
    return {
        "id": 99,
        "name": name,
        "can_admins_bypass": bypass,
        "protection_rules": rules,
        "deployment_branch_policy": policy,
    }


class FakeGitHub:
    """Answers github.request like the environments API of o/r would."""

    def __init__(
        self,
        existing: list[dict[str, Any]],
        policies: dict[str, list[dict[str, Any]]] | None = None,
        *,
        ignored: frozenset[str] = frozenset(),
    ) -> None:
        self.existing = {env["name"]: env for env in existing}
        self.policies = {name: list(items) for name, items in (policies or {}).items()}
        self.ignored = ignored
        self.next_id = 100
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        assert token == "tok"
        self.calls.append((method, url, json_body))
        path, _, query = url.partition("?")
        page = int(urllib.parse.parse_qs(query)["page"][0]) if query else 1
        start, end = (page - 1) * environments.PER_PAGE, page * environments.PER_PAGE
        if path.startswith(f"{github.API_URL}/users/"):
            return {"id": USERS[path.rsplit("/", 1)[1]]}
        if path.startswith(f"{github.API_URL}/orgs/o/teams/"):
            return {"id": TEAMS[path.rsplit("/", 1)[1]]}
        assert path.startswith(API)
        rest = [urllib.parse.unquote(part) for part in path.removeprefix(API).split("/")[1:]]
        if not rest:
            assert method == "GET"
            return {"total_count": len(self.existing), "environments": list(self.existing.values())[start:end]}
        name = rest[0]
        if len(rest) == 1:
            assert method == "PUT"
            return self._put(name, json_body)
        policies = self.policies.setdefault(name, [])
        if method == "GET":
            return {"total_count": len(policies), "branch_policies": policies[start:end]}
        if method == "POST":
            self.next_id += 1
            policies.append({"id": self.next_id, **json_body})
            return policies[-1]
        assert method == "DELETE"
        self.policies[name] = [policy for policy in policies if policy["id"] != int(rest[2])]
        return None

    def _put(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        body = {key: value for key, value in body.items() if key not in self.ignored}
        old = self.existing.get(name, _env(name))
        old_reviewers = [rule for rule in old["protection_rules"] if rule["type"] == "required_reviewers"]
        policy = body.get("deployment_branch_policy", old["deployment_branch_policy"])
        deployment = "all" if policy is None else "protected" if policy["protected_branches"] else "selected"
        if deployment != "selected":
            self.policies.pop(name, None)
        reviewers = body.get("reviewers", "keep")
        if reviewers == "keep":
            pairs = [(r["type"], r["reviewer"]["id"]) for rule in old_reviewers for r in rule["reviewers"]]
        else:
            pairs = [(r["type"], r["id"]) for r in reviewers or []]
        self.existing[name] = _env(
            name,
            deployment=deployment,
            wait=body.get("wait_timer", 0),
            reviewers=pairs,
            prevent=body.get("prevent_self_review", False),
            bypass=body.get("can_admins_bypass", old["can_admins_bypass"]),
        )
        return self.existing[name]

    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]


def _policy(ident: int, name: str, kind: str | None = "tag") -> dict[str, Any]:
    policy: dict[str, Any] = {"id": ident, "name": name}
    if kind is not None:
        policy["type"] = kind
    return policy


def _matching() -> FakeGitHub:
    return FakeGitHub(
        [
            _env("pypi", deployment="selected", reviewers=[("User", 1), ("Team", 10)], prevent=True),
            _env("testpypi", wait=5),
            _env("docs", deployment="protected"),
            _env("github-pages"),
        ],
        {"pypi": [_policy(1, "v*")]},
    )


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
    return environments.main([*argv, "--repo", "o/r"])


@pytest.mark.parametrize("mode", ["check", "ensure"])
def test_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    gh = _matching()
    assert _run(tmp_path, monkeypatch, gh, [mode]) == 0
    assert gh.writes() == []
    expected = {
        "check": "All 3 environment(s) in o/r match pyproject.toml.\n",
        "ensure": "Created or updated 0 of 3 environment(s) in o/r.\n",
    }
    assert capsys.readouterr().out == expected[mode]


def test_check_reports_differences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(
        [_env("PyPI", reviewers=[("User", 2)]), _env("testpypi", deployment="protected", wait=5)],
        {"testpypi": [_policy(1, "main", "branch")]},
    )
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 1
    assert gh.writes() == []
    assert capsys.readouterr().out == (
        "pypi: needs updating: deployment-branches is 'all' on GitHub, not 'selected'; "
        "allowed-tags is [] on GitHub, not ['v*']; reviewers is ['bob'] on GitHub, not ['alice', 'o/devs']; "
        "prevent-self-review is False on GitHub, not True\n"
        "pypi: name is 'PyPI' on GitHub (environments can't be renamed)\n"
        "testpypi: needs updating: deployment-branches is 'protected' on GitHub, not 'all'\n"
        "docs: missing\n"
        "3 of 3 environment(s) in o/r need `repoutils environments ensure`.\n"
    )


def test_check_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub([]), ["check", "--exit-zero"]) == 0
    assert "3 of 3 environment(s) in o/r need" in capsys.readouterr().out


def test_check_fail_on_unexpected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = _matching()
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"]) == 1
    assert capsys.readouterr().out == (
        "github-pages: not defined in pyproject.toml\n"
        "1 environment(s) in o/r aren't defined in pyproject.toml: "
        "add them there (`repoutils environments dump` prints them) or delete them on GitHub.\n"
    )
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected", "--exit-zero"]) == 0


def test_check_fail_on_unexpected_ignores_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = '[tool.repoutils.environments."GitHub-Pages"]\n'
    assert _run(tmp_path, monkeypatch, FakeGitHub([_env("github-pages")]), ["check", "--fail-on-unexpected"]) != 0
    gh = FakeGitHub([_env("github-pages")])
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], config) == 0


def test_ensure_creates_and_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(
        [_env("pypi", deployment="selected", wait=3, bypass=False), _env("testpypi", deployment="selected")],
        {"pypi": [_policy(1, "main", "branch"), _policy(2, "v*"), _policy(3, "old*")]},
    )
    assert _run(tmp_path, monkeypatch, gh, ["ensure"]) == 0
    assert gh.writes() == [
        (
            "PUT",
            f"{API}/pypi",
            {
                # The settings pyproject.toml leaves out are sent as GitHub has them.
                "wait_timer": 3,
                "prevent_self_review": True,
                "reviewers": [{"type": "User", "id": 1}, {"type": "Team", "id": 10}],
                "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
                "can_admins_bypass": False,
            },
        ),
        ("DELETE", f"{API}/pypi/deployment-branch-policies/1", None),
        ("DELETE", f"{API}/pypi/deployment-branch-policies/3", None),
        (
            "PUT",
            f"{API}/testpypi",
            {
                "wait_timer": 5,
                "prevent_self_review": False,
                "reviewers": None,
                "deployment_branch_policy": None,
                "can_admins_bypass": True,
            },
        ),
        (
            "PUT",
            f"{API}/docs",
            {
                "wait_timer": 0,
                "prevent_self_review": False,
                "reviewers": None,
                "deployment_branch_policy": None,
                "can_admins_bypass": True,
            },
        ),
    ]
    out = capsys.readouterr().out
    assert out.startswith("pypi: updated: allowed-branches is ['main'] on GitHub, not []; ")
    assert "testpypi: updated: deployment-branches is 'selected' on GitHub, not 'all'; wait-timer is 0" in out
    assert "docs: created\n" in out
    assert out.endswith("Created or updated 3 of 3 environment(s) in o/r.\n")
    assert _run(tmp_path, monkeypatch, gh, ["check"]) == 0


def test_ensure_restricts_an_open_environment_to_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Issue #3: only a v* tag may deploy to pypi.
    config = '[tool.repoutils.environments.pypi]\nallowed-tags = ["v*"]\n'
    gh = FakeGitHub([_env("pypi")])
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], config) == 0
    assert [(method, url.removeprefix(API)) for method, url, _ in gh.writes()] == [
        ("PUT", "/pypi"),
        ("POST", "/pypi/deployment-branch-policies"),
    ]
    assert gh.writes()[1][2] == {"name": "v*", "type": "tag"}
    assert gh.policies["pypi"] == [{"id": 101, "name": "v*", "type": "tag"}]


def test_untyped_policies_are_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = '[tool.repoutils.environments.pypi]\nallowed-branches = ["main"]\n'
    gh = FakeGitHub([_env("pypi", deployment="selected")], {"pypi": [_policy(1, "main", None)]})
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], config) == 0
    assert gh.writes() == []


def test_ensure_quotes_names_in_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = '[tool.repoutils.environments."a b/c"]\n'
    gh = FakeGitHub([])
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], config) == 0
    assert gh.writes()[0][1] == f"{API}/a%20b%2Fc"


def test_ensure_fails_if_github_ignores_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([_env("testpypi")], ignored=frozenset({"wait_timer"}))
    config = "[tool.repoutils.environments.testpypi]\nwait-timer = 5\n"
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], config) == 1
    assert capsys.readouterr().err == ("GitHub didn't apply the change: testpypi: wait-timer is 0 on GitHub, not 5\n")


def test_reads_every_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    names = [f"env{i}" for i in range(environments.PER_PAGE + 1)]
    gh = FakeGitHub(
        [_env(name) for name in names[:-1]] + [_env("last", deployment="selected")],
        {"last": [_policy(i, f"v{i}") for i in range(environments.PER_PAGE + 1)]},
    )
    tags = ", ".join(f'"v{i}"' for i in range(environments.PER_PAGE + 1))
    config = f"[tool.repoutils.environments.last]\nallowed-tags = [{tags}]\n"
    assert _run(tmp_path, monkeypatch, gh, ["check"], config) == 0


def test_unknown_reviewer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env = _env("pypi")
    env["protection_rules"] = [{"type": "required_reviewers", "reviewers": [{"type": "Bot", "reviewer": {}}]}]
    assert _run(tmp_path, monkeypatch, FakeGitHub([env]), ["dump"], "") == 1
    assert "required reviewer repoutils doesn't understand" in capsys.readouterr().err


def test_no_environments_defined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([])
    assert _run(tmp_path, monkeypatch, gh, ["check"], "[project]\nname = 'x'\n") == 1
    assert gh.calls == []
    assert "No environments are defined" in capsys.readouterr().err


def test_http_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def request(*args: object, **kwargs: object) -> Any:
        raise urllib.error.HTTPError("https://x/environments", 404, "Not Found", Message(), io.BytesIO(b"nope"))

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(github, "request", request)
    # --exit-zero ignores environments needing ensure, but not an error.
    assert environments.main(["check", "--exit-zero", "--repo", "o/r"]) == 1
    assert capsys.readouterr().err == "HTTP 404 from https://x/environments:\nnope\n"


def test_gh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def gh_output(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="not logged in")

    (tmp_path / "pyproject.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(github, "gh_output", gh_output)
    assert environments.main(["check"]) == 1
    assert capsys.readouterr().err == "gh auth token failed:\nnot logged in\n"


TABLE = "[tool.repoutils.environments.pypi]\n"


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ("[tool.repoutils]\nenvironments = 1\n", "must be a table"),
        ("[tool.repoutils.environments]\npypi = 1\n", "[tool.repoutils.environments.pypi] must be a table"),
        (
            '[tool.repoutils.environments.pypi]\n[tool.repoutils.environments."PyPI"]\n',
            "[tool.repoutils.environments.PyPI] is defined twice",
        ),
        (TABLE + "branches = []\n", "unknown setting: branches"),
        (TABLE + "deployment-branches = 'tags'\n", 'must be one of "all", "protected", "selected"'),
        (TABLE + "deployment-branches = 'all'\nallowed-tags = []\n", 'need deployment-branches = "selected"'),
        (TABLE + "allowed-tags = 'v*'\n", "allowed-tags must be a list of non-empty strings"),
        (TABLE + "allowed-tags = ['']\n", "allowed-tags must be a list of non-empty strings"),
        (TABLE + "allowed-tags = ['v*', 'v*']\n", "allowed-tags lists something twice"),
        (TABLE + "wait-timer = -1\n", "wait-timer must be a whole number of minutes from 0 to 43200"),
        (TABLE + "wait-timer = 43201\n", "wait-timer must be"),
        (TABLE + "wait-timer = true\n", "wait-timer must be"),
        (TABLE + "reviewers = ['a', 'b', 'c', 'd', 'e', 'f', 'g']\n", "reviewers can list at most 6"),
        (TABLE + "reviewers = ['@alice']\n", 'reviewers must list a user as "login"'),
        (TABLE + "reviewers = ['a/b/c']\n", 'reviewers must list a user as "login"'),
        (TABLE + "reviewers = ['Alice', 'alice']\n", "reviewers lists something twice"),
        (TABLE + "prevent-self-review = 1\n", "prevent-self-review must be true or false"),
        (TABLE + "can-admins-bypass = 'no'\n", "can-admins-bypass must be true or false"),
    ],
)
def test_bad_config(tmp_path: Path, config: str, error: str) -> None:
    (tmp_path / "pyproject.toml").write_text(config)
    with pytest.raises(ValueError, match=re.escape(error)):
        environments.load_environments(tmp_path)


def test_load_environments(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(CONFIG)
    assert environments.load_environments(tmp_path) == [
        Environment(
            "pypi",
            {
                "allowed-tags": ["v*"],
                "allowed-branches": [],
                "reviewers": ["alice", "o/devs"],
                "prevent-self-review": True,
                # Implied by the allowed branches and tags.
                "deployment-branches": "selected",
            },
        ),
        Environment("testpypi", {"deployment-branches": "all", "wait-timer": 5}),
        Environment("docs", {}),
    ]


def test_bad_config_through_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub([])
    assert _run(tmp_path, monkeypatch, gh, ["ensure"], TABLE + "name = 'x'\n") == 1
    assert gh.calls == []
    assert "unknown setting: name" in capsys.readouterr().err


def test_dump_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(
        [
            _env("pypi", deployment="selected", wait=10, reviewers=[("Team", 10), ("User", 1)], prevent=True),
            _env("github pages", deployment="protected", bypass=False),
        ],
        {"pypi": [_policy(1, "v*"), _policy(2, "release/*", "branch"), _policy(3, 'a"b')]},
    )
    # dump neither reads pyproject.toml (this one is malformed) nor changes it.
    config = "[tool.repoutils]\nenvironments = 1\n"
    assert _run(tmp_path, monkeypatch, gh, ["dump"], config) == 0
    assert (tmp_path / "pyproject.toml").read_text() == config
    out, err = capsys.readouterr()
    assert err == ""
    assert out == (
        "[tool.repoutils.environments.pypi]\n"
        'deployment-branches = "selected"\n'
        'allowed-branches = ["release/*"]\n'
        'allowed-tags = ["a\\"b", "v*"]\n'
        "wait-timer = 10\n"
        'reviewers = ["alice", "o/devs"]\n'
        "prevent-self-review = true\n"
        "can-admins-bypass = true\n"
        "\n"
        '[tool.repoutils.environments."github pages"]\n'
        'deployment-branches = "protected"\n'
        "wait-timer = 0\n"
        "reviewers = []\n"
        "prevent-self-review = false\n"
        "can-admins-bypass = false\n"
    )
    # Pasted into pyproject.toml, it gives every environment as GitHub has it.
    assert _run(tmp_path, monkeypatch, gh, ["check", "--fail-on-unexpected"], out) == 0
    assert gh.writes() == []


def test_dump_no_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, monkeypatch, FakeGitHub([]), ["dump"], "") == 0
    assert capsys.readouterr() == ("", "o/r has no environments.\n")


def test_patterns_differing_only_in_case_are_distinct(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(TABLE + "allowed-branches = ['main', 'Main']\n")
    assert environments.load_environments(tmp_path)[0].settings["allowed-branches"] == ["main", "Main"]

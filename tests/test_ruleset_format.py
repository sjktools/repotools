from pathlib import Path
from typing import Any

import pytest

from repoutils import config
from repoutils.ruleset_format import RULES, Names, diff, dump_toml, from_github, load_rulesets, to_github
from repoutils.ruleset_labels import LABELS

CONFIG = """
[tool.repoutils.rulesets.main]
include = ["~DEFAULT_BRANCH", "refs/heads/release/*"]
bypass = [{ role = "admin", mode = "pull-request" }, { app = "dependabot" }, { team = "core", mode = "exempt" }]

[tool.repoutils.rulesets.main.rules]
deletion = true
creation = false
non-fast-forward = true

[tool.repoutils.rulesets.main.rules.pull-request]
allowed-merge-methods = ["squash"]
# A default, so left out.
dismiss-stale-reviews-on-push = false
required-approving-review-count = 1

[tool.repoutils.rulesets.main.rules.required-status-checks]
required-status-checks = [{ context = "test", app = "github-actions" }, { context = "lint" }]

[tool.repoutils.rulesets."Release tags"]
enforcement = "disabled"
include = ["~ALL"]
exclude = ["old"]
bypass = [{ org-admin = true }, { deploy-keys = true }, { role = 7 }]

[tool.repoutils.rulesets."Release tags".rules.commit-message-pattern]
operator = "starts_with"
pattern = "fix"
"""
NAMES = Names(apps={"github-actions": 15368, "dependabot": 29110}, teams={"core": 42})


def _load(tmp_path: Path, text: str) -> dict[str, dict[str, Any]]:
    (tmp_path / "pyproject.toml").write_text(text)
    return load_rulesets(tmp_path)


def test_load_keeps_only_non_defaults(tmp_path: Path) -> None:
    rulesets = _load(tmp_path, CONFIG)
    assert rulesets["main"] == {
        "include": ["release/*", "~DEFAULT_BRANCH"],
        # Sorted, so their order doesn't matter.
        "bypass": [
            {"app": "dependabot"},
            {"team": "core", "mode": "exempt"},
            {"role": "admin", "mode": "pull-request"},
        ],
        "rules": {
            "deletion": {},
            "non-fast-forward": {},
            "pull-request": {"allowed-merge-methods": ["squash"], "required-approving-review-count": 1},
            "required-status-checks": {
                "required-status-checks": [{"context": "test", "app": "github-actions"}, {"context": "lint"}]
            },
        },
    }
    assert rulesets["Release tags"]["enforcement"] == "disabled"
    assert rulesets["Release tags"]["rules"] == {
        "commit-message-pattern": {"operator": "starts_with", "pattern": "fix"}
    }


def test_no_rulesets(tmp_path: Path) -> None:
    assert _load(tmp_path, "") == {}
    assert _load(tmp_path, "[tool.repoutils.rulesets.x]\ninclude = ['~ALL']\n") == {"x": {"include": ["~ALL"]}}


def test_to_github_sends_every_parameter(tmp_path: Path) -> None:
    body = to_github("main", _load(tmp_path, CONFIG)["main"], NAMES)
    assert body["conditions"] == {"ref_name": {"include": ["refs/heads/release/*", "~DEFAULT_BRANCH"], "exclude": []}}
    assert body["bypass_actors"] == [
        {"actor_id": 29110, "actor_type": "Integration", "bypass_mode": "always"},
        {"actor_id": 42, "actor_type": "Team", "bypass_mode": "exempt"},
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"},
    ]
    rules = {rule["type"]: rule.get("parameters") for rule in body["rules"]}
    assert rules["deletion"] is None
    assert rules["pull_request"] == {
        "allowed_merge_methods": ["squash"],
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_approving_review_count": 1,
        "required_review_thread_resolution": False,
    }
    assert rules["required_status_checks"] == {
        "do_not_enforce_on_create": False,
        "required_status_checks": [{"context": "test", "integration_id": 15368}, {"context": "lint"}],
        "strict_required_status_checks_policy": False,
    }
    other = to_github("Release tags", _load(tmp_path, CONFIG)["Release tags"], NAMES)
    assert other["enforcement"] == "disabled"
    assert other["bypass_actors"] == [
        {"actor_id": None, "actor_type": "DeployKey", "bypass_mode": "always"},
        {"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"},
        {"actor_id": 7, "actor_type": "RepositoryRole", "bypass_mode": "always"},
    ]
    assert other["rules"] == [
        {
            "type": "commit_message_pattern",
            "parameters": {"operator": "starts_with", "pattern": "fix", "negate": False},
        }
    ]


def test_round_trip(tmp_path: Path) -> None:
    rulesets = _load(tmp_path, CONFIG)
    for name, ruleset in rulesets.items():
        assert from_github(to_github(name, ruleset, NAMES), NAMES) == ruleset
    text, warnings = dump_toml([to_github(name, r, NAMES) for name, r in rulesets.items()], NAMES)
    assert warnings == []
    assert '[tool.repoutils.rulesets."Release tags"]\n' in text
    assert "[tool.repoutils.rulesets.main.rules]\n# Restrict deletions\ndeletion = true\n" in text
    assert "# Block force pushes\nnon-fast-forward = true\n" in text
    assert "# Require a pull request before merging\n[tool.repoutils.rulesets.main.rules.pull-request]\n" in text
    assert "# Required approvals\nrequired-approving-review-count = " in text
    assert _load(tmp_path, text) == rulesets


def test_from_github_ignores_unknown_parameters_and_defaults() -> None:
    ruleset = {
        "name": "x",
        "enforcement": "active",
        "bypass_actors": [],
        "rules": [
            {"type": "update", "parameters": {"update_allows_fetch_and_merge": False, "something_new": True}},
            {"type": "required_status_checks", "parameters": {"required_status_checks": None}},
        ],
    }
    assert from_github(ruleset, Names()) == {"rules": {"update": {}, "required-status-checks": {}}}


def test_from_github_needs_the_bypass_list() -> None:
    with pytest.raises(RuntimeError, match="bypass list of ruleset 'x'"):
        from_github({"name": "x", "rules": []}, Names())


def test_dump_warns_about_what_check_refuses() -> None:
    ruleset = {
        "name": "x",
        "bypass_actors": [
            {"actor_id": 99, "actor_type": "Integration", "bypass_mode": "always"},
            {"actor_id": 98, "actor_type": "Team", "bypass_mode": "always"},
            {"actor_id": 3, "actor_type": "EnterpriseOwner", "bypass_mode": "always"},
        ],
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "c", "integration_id": 99}]},
            },
            {"type": "brand_new_rule", "parameters": {"some_setting": 1}},
        ],
    }
    text, warnings = dump_toml([ruleset], Names())
    assert "{ app-id = 99 }" in text
    assert '{ context = "c", integration-id = 99 }' in text
    assert "[tool.repoutils.rulesets.x.rules.brand-new-rule]\nsome-setting = 1\n" in text
    assert warnings == [
        "x: repoutils doesn't know the bypass actor type EnterpriseOwner",
        "x: couldn't find the slug of app 99, so gave its ID, which check refuses: replace it with app = <its slug>",
        "x: couldn't find the slug of team 98, so gave its ID, which check refuses: replace it with team = <its slug>",
        "x: repoutils doesn't know the rule brand-new-rule",
        "x: it targets no branch, so check refuses it without an include",
    ]


def test_diff() -> None:
    ours = {"include": ["main"], "rules": {"deletion": {}, "pull-request": {"required-approving-review-count": 1}}}
    theirs = {"enforcement": "disabled", "rules": {"pull-request": {}, "non-fast-forward": {}}}
    assert diff(theirs, ours) == [
        'enforcement is "disabled" on GitHub, not "active"',
        "include: GitHub lacks:",
        '  "main"',
        "rule deletion is off on GitHub",
        "rule pull-request required-approving-review-count is 0 on GitHub, not 1",
        "rule non-fast-forward is on on GitHub, but pyproject.toml leaves it off",
    ]
    assert diff(ours, ours) == []


def test_diff_names_only_the_list_items_that_differ() -> None:
    def checks(*contexts: str) -> dict[str, Any]:
        return {"rules": {"required-status-checks": {"required-status-checks": [{"context": c} for c in contexts]}}}

    assert diff(checks("a", "b", "old"), checks("a", "b", "new", "newer")) == [
        "rule required-status-checks required-status-checks: GitHub lacks:",
        '  { context = "new" }',
        '  { context = "newer" }',
        "rule required-status-checks required-status-checks: GitHub also has:",
        '  { context = "old" }',
    ]
    # An unknown rule's parameter has no default.
    assert diff({"rules": {"x": {}}}, {"rules": {"x": {"p": 1}}}) == ["rule x p is None on GitHub, not 1"]


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('main = "x"', "rulesets.main] must be a table"),
        ("main.target = 'push'", 'target must be one of "branch", "tag" (repoutils doesn\'t manage push rulesets)'),
        ("main.target = []", 'target must be one of "branch", "tag"'),
        ("main.target = 'tag'\nmain.include = []", 'needs include, the tags it targets (e.g. ["v*"])'),
        ("main.target = 'tag'\nmain.include = ['~DEFAULT_BRANCH']", "names ~DEFAULT_BRANCH, which isn't a tag"),
        ("main.target = 'tag'\nmain.include = ['refs/heads/main']", "names refs/heads/main, which isn't a tag"),
        ("main.include = ['refs/tags/v*']", "names refs/tags/v*, which isn't a branch"),
        (
            "main.target = 'tag'\nmain.include = ['v*']\nmain.rules.pull-request = true",
            "rules.pull-request applies only to branch rulesets, and this is a tag one",
        ),
        (
            "main.include = ['main']\nmain.rules.tag-name-pattern = { operator = 'regex', pattern = 'x' }",
            "rules.tag-name-pattern applies only to tag rulesets, and this is a branch one",
        ),
        ("main.colour = 1", "unknown setting(s): colour"),
        ("main.enforcement = 'on'", 'enforcement must be one of "active", "evaluate", "disabled"'),
        ("main.include = 'main'", "include must be a list of strings"),
        ("main.include = []", 'needs include, the branches it targets (e.g. ["~DEFAULT_BRANCH"])'),
        ("main.bypass = [{ role = 'admin', mode = ['always'] }]", "mode must be one of"),
        ("main.bypass = [{ role = [] }]", 'role must be "admin"'),
        ("main.bypass = {}", "bypass must be a list of tables"),
        ("main.bypass = ['admin']", "bypass must be a list of tables"),
        (
            "main.bypass = [{ role = 'admin', mode = 'never' }]",
            'mode must be one of "always", "pull-request", "exempt"',
        ),
        ("main.bypass = [{ app-id = 1 }]", "by its slug"),
        ("main.bypass = [{ role = 'admin', app = 'x' }]", "exactly one of role, app, team, org-admin, deploy-keys"),
        ("main.bypass = [{ role = 'owner' }]", 'role must be "admin", "maintain", "write"'),
        ("main.bypass = [{ app = '' }]", "app must be a slug"),
        ("main.bypass = [{ org-admin = false }]", "org-admin must be true"),
        ("main.rules = 1", "rules must be a table"),
        ("main.rules.non_fast_forward = true", "unknown rule non_fast_forward"),
        ("main.rules.deletion = 1", "rules.deletion must be true, false or a table"),
        ("main.rules.deletion = { x = 1 }", "unknown parameter x (it has: none)"),
        ("main.rules.update = { allow = true }", "(it has: update-allows-fetch-and-merge)"),
        ("main.rules.update = { update-allows-fetch-and-merge = 1 }", "must be true or false"),
        ("main.rules.pull-request = { required-approving-review-count = -1 }", "must be a whole number"),
        ("main.rules.merge-queue = { merge-method = 1 }", "merge-method must be a string"),
        ("main.rules.pull-request = { allowed-merge-methods = 'squash' }", "must be a list"),
        ("main.rules.pull-request = { allowed-merge-methods = [1] }", "must be a list of strings"),
        ("main.rules.workflows = { workflows = ['x'] }", "must be a list of tables"),
        ("main.rules.max-file-size = {}", "rules.max-file-size needs max-file-size"),
        ("main.rules.required-status-checks.required-status-checks = ['x']", "must be a list of tables"),
        (
            "main.rules.required-status-checks.required-status-checks = [{ context = 'x', integration-id = 1 }]",
            "not its ID",
        ),
        ("main.rules.required-status-checks.required-status-checks = [{ name = 'x' }]", "unknown setting(s): name"),
        ("main.rules.required-status-checks.required-status-checks = [{ app = 'x' }]", "each check needs a context"),
    ],
)
def test_malformed(tmp_path: Path, table: str, message: str) -> None:
    with pytest.raises(config.ConfigError) as error:
        _load(tmp_path, f"[tool.repoutils.rulesets]\n{table}\n")
    assert message in str(error.value)


def test_every_label_names_a_known_rule_or_parameter() -> None:
    for key in LABELS:
        rule, _, param = key.partition(".")
        assert rule in RULES and (not param or param in RULES[rule]), key


TAG_CONFIG = """
[tool.repoutils.rulesets.tags]
target = "tag"
include = ["refs/tags/v*", "~ALL"]
exclude = ["v0.*"]
bypass = [{ deploy-keys = true }]

[tool.repoutils.rulesets.tags.rules]
creation = true
required-signatures = true

[tool.repoutils.rulesets.tags.rules.tag-name-pattern]
operator = "regex"
pattern = "^v[0-9]"

[tool.repoutils.rulesets.tags.rules.required-status-checks]
required-status-checks = [{ context = "test", app = "github-actions" }]
"""


def test_tag_ruleset(tmp_path: Path) -> None:
    ruleset = _load(tmp_path, TAG_CONFIG)["tags"]
    assert ruleset["target"] == "tag"
    assert ruleset["include"] == ["v*", "~ALL"]
    body = to_github("tags", ruleset, NAMES)
    assert body["target"] == "tag"
    assert body["conditions"] == {"ref_name": {"include": ["refs/tags/v*", "~ALL"], "exclude": ["refs/tags/v0.*"]}}
    assert body["rules"][2] == {
        "type": "tag_name_pattern",
        "parameters": {"operator": "regex", "pattern": "^v[0-9]", "negate": False},
    }
    assert from_github(body, NAMES) == ruleset
    text, warnings = dump_toml([body], NAMES)
    assert warnings == []
    assert text.startswith('[tool.repoutils.rulesets.tags]\ntarget = "tag"\ninclude = ["v*", "~ALL"]\n')
    assert "# Restrict tag names\n[tool.repoutils.rulesets.tags.rules.tag-name-pattern]\n" in text
    assert _load(tmp_path, text) == {"tags": ruleset}


def test_dump_warns_about_rules_a_target_cant_have() -> None:
    rules = [{"type": "non_fast_forward"}, {"type": "pull_request"}]
    ruleset: dict[str, Any] = {"name": "x", "target": "tag", "bypass_actors": [], "rules": rules}
    _, warnings = dump_toml([ruleset], Names())
    assert warnings == [
        "x: check refuses rule pull-request in a tag ruleset",
        "x: it targets no tag, so check refuses it without an include",
    ]
    _, warnings = dump_toml([ruleset | {"target": "push"}], Names())
    assert warnings[0] == "x: repoutils doesn't manage push rulesets"

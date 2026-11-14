"""The pyproject.toml format of a branch or tag ruleset (``[tool.repoutils.rulesets.<name>]``),
which gives only what differs from a new ruleset's defaults, and its
conversion to and from the JSON GitHub's API and ruleset exports use, for
``repoutils rulesets``. Stdlib-only, like every repoutils module."""

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from repoutils import config
from repoutils.ruleset_labels import LABELS

TABLE = "[tool.repoutils.rulesets]"
ENFORCEMENTS = ("active", "evaluate", "disabled")
# The kinds of ruleset repoutils manages, with the prefix of the full refs each one's patterns name.
TARGETS = {"branch": "refs/heads/", "tag": "refs/tags/"}
# Each bypass mode as pyproject.toml gives it, and as GitHub does.
BYPASS_MODES = {"always": "always", "pull-request": "pull_request", "exempt": "exempt"}
# The IDs of the base repository roles that can bypass a ruleset (a custom role is given by its ID).
ROLES = {"maintain": 2, "write": 4, "admin": 5}
ACTOR_KEYS = ("role", "app", "team", "org-admin", "deploy-keys")
# The order of a ruleset's keys in pyproject.toml.
KEYS = ("target", "enforcement", "include", "exclude", "bypass", "rules")
# The settings' defaults, which load_rulesets and from_github leave out.
DEFAULTS: dict[str, Any] = {"target": "branch", "enforcement": "active"}
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Required:
    """A rule parameter with no default, of this type."""

    kind: type


_PATTERN = {"operator": Required(str), "pattern": Required(str), "name": "", "negate": False}
# Every rule a branch or tag ruleset can have, by its API name, with each of its
# parameters' defaults: what GitHub's ruleset page gives a new rule.
RULES: dict[str, dict[str, Any]] = {
    "creation": {},
    "update": {"update_allows_fetch_and_merge": False},
    "deletion": {},
    "required_linear_history": {},
    "merge_queue": {
        "check_response_timeout_minutes": 60,
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 5,
        "max_entries_to_merge": 5,
        "merge_method": "MERGE",
        "min_entries_to_merge": 1,
        "min_entries_to_merge_wait_minutes": 5,
    },
    "required_deployments": {"required_deployment_environments": []},
    "required_signatures": {},
    "pull_request": {
        "allowed_merge_methods": ["merge", "squash", "rebase"],
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": False,
        "required_reviewers": [],
    },
    "required_status_checks": {
        "do_not_enforce_on_create": False,
        "required_status_checks": [],
        "strict_required_status_checks_policy": False,
    },
    "non_fast_forward": {},
    "commit_message_pattern": _PATTERN,
    "commit_author_email_pattern": _PATTERN,
    "committer_email_pattern": _PATTERN,
    "branch_name_pattern": _PATTERN,
    "tag_name_pattern": _PATTERN,
    "file_path_restriction": {"restricted_file_paths": []},
    "max_file_path_length": {"max_file_path_length": Required(int)},
    "file_extension_restriction": {"restricted_file_extensions": []},
    "max_file_size": {"max_file_size": Required(int)},
    "workflows": {"do_not_enforce_on_create": False, "workflows": []},
    "code_scanning": {"code_scanning_tools": []},
    "copilot_code_review": {"review_draft_pull_requests": False, "review_on_push": False},
}
# The rules a tag ruleset can have: GitHub's ruleset page offers the rest for branches only.
TAG_RULES = {
    "creation",
    "update",
    "deletion",
    "required_linear_history",
    "required_deployments",
    "required_signatures",
    "non_fast_forward",
    "commit_message_pattern",
    "commit_author_email_pattern",
    "committer_email_pattern",
    "tag_name_pattern",
}
# The rules a branch ruleset can have.
BRANCH_RULES = set(RULES) - {"tag_name_pattern"}
ALLOWED_RULES = {"branch": BRANCH_RULES, "tag": TAG_RULES}
# Parameters ensure sends only when pyproject.toml gives them (the rest it always sends).
OPTIONAL = {"required_reviewers", "name"}
# Parameters that are lists of tables; the rest that are lists are lists of strings.
TABLE_LISTS = {"required_status_checks", "required_reviewers", "workflows", "code_scanning_tools"}


def _kebab(name: str) -> str:
    return name.replace("_", "-")


def _snake(name: str) -> str:
    return name.replace("-", "_")


def _keys(value: Any, convert: Callable[[str], str]) -> Any:
    """value with every table key in it converted."""
    if isinstance(value, dict):
        return {convert(k): _keys(v, convert) for k, v in value.items()}
    if isinstance(value, list):
        return [_keys(v, convert) for v in value]
    return value


def _normal(value: Any) -> Any:
    """value with every list sorted and every null dropped, so two values compare equal regardless of order."""
    if isinstance(value, dict):
        return {k: _normal(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return sorted((_normal(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def short_ref(pattern: str, target: str = "branch") -> str:
    return pattern.removeprefix(TARGETS[target])


def _full_ref(pattern: str, target: str) -> str:
    return pattern if pattern.startswith(("~", "refs/")) else f"{TARGETS[target]}{pattern}"


def _toml_key(name: str) -> str:
    return name if _BARE_KEY_RE.fullmatch(name) else config.toml_string(name)


def _toml(value: Any) -> str:
    """value as an inline TOML value."""
    if isinstance(value, bool | str):
        return config.toml_value(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml(v)}" for k, v in value.items()) + " }" if value else "{}"
    return str(value)


def _toml_block(value: Any) -> str:
    """value as TOML, with a list of tables one to a line."""
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return "[\n" + "".join(f"    {_toml(v)},\n" for v in value) + "]"
    return _toml(value)


# Loading pyproject.toml.


def _default(rule_type: str, param: str) -> Any:
    return _normal(RULES[rule_type][param])


def _compact(rule_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """params (kebab case) without those that have their default."""
    schema = RULES[rule_type]
    return {
        k: v
        for k, v in params.items()
        if isinstance(schema[_snake(k)], Required) or v != _default(rule_type, _snake(k))
    }


def _load_check(where: str, entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise config.ConfigError(f"{where} must be a list of tables")
    if "integration-id" in entry:
        raise config.ConfigError(f'{where}: give a check\'s app by its slug (app = "github-actions"), not its ID')
    unknown = sorted(set(entry) - {"context", "app"})
    if unknown:
        raise config.ConfigError(f"{where} has unknown setting(s): {', '.join(unknown)}")
    if "context" not in entry or not all(
        isinstance(entry.get(k, "x"), str) and entry.get(k, "x") for k in ("context", "app")
    ):
        raise config.ConfigError(f"{where}: each check needs a context, and its app (if any), as non-empty strings")
    return {k: entry[k] for k in ("context", "app") if k in entry}


def _load_value(where: str, param: str, kind: type, value: Any) -> Any:
    if kind is bool and type(value) is not bool:
        raise config.ConfigError(f"{where} must be true or false")
    if kind is int and (type(value) is not int or value < 0):
        raise config.ConfigError(f"{where} must be a whole number")
    if kind is str and not isinstance(value, str):
        raise config.ConfigError(f"{where} must be a string")
    if kind is list:
        if not isinstance(value, list):
            raise config.ConfigError(f"{where} must be a list")
        if param == "required_status_checks":
            value = [_load_check(where, entry) for entry in value]
        elif param in TABLE_LISTS and not all(isinstance(entry, dict) for entry in value):
            raise config.ConfigError(f"{where} must be a list of tables")
        elif param not in TABLE_LISTS and not all(isinstance(entry, str) for entry in value):
            raise config.ConfigError(f"{where} must be a list of strings")
    return _normal(value)


def _load_rule(where: str, rule_type: str, value: dict[str, Any]) -> dict[str, Any]:
    schema = RULES[rule_type]
    params = {}
    for key, v in value.items():
        default = schema.get(_snake(key))
        if "_" in key or default is None:
            known = ", ".join(_kebab(param) for param in schema) or "none"
            raise config.ConfigError(f"{where} has an unknown parameter {key} (it has: {known})")
        kind = default.kind if isinstance(default, Required) else type(default)
        params[key] = _load_value(f"{where}.{key}", _snake(key), kind, v)
    missing = [_kebab(p) for p, d in schema.items() if isinstance(d, Required) and _kebab(p) not in params]
    if missing:
        raise config.ConfigError(f"{where} needs {', '.join(missing)}")
    return _compact(rule_type, params)


def _load_actor(where: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise config.ConfigError(f"{where} must be a list of tables")
    mode = entry.get("mode", "always")
    if not isinstance(mode, str) or mode not in BYPASS_MODES:
        raise config.ConfigError(f"{where} mode must be one of {', '.join(map(config.toml_string, BYPASS_MODES))}")
    actor = {k: v for k, v in entry.items() if k != "mode"}
    if "app-id" in actor or "team-id" in actor:
        raise config.ConfigError(f'{where}: give an app or team by its slug (app = "dependabot"), not its ID')
    if len(actor) != 1 or next(iter(actor)) not in ACTOR_KEYS:
        raise config.ConfigError(f"{where}: each entry needs exactly one of {', '.join(ACTOR_KEYS)}, and maybe mode")
    ((key, value),) = actor.items()
    if key == "role" and not ((isinstance(value, str) and value in ROLES) or (type(value) is int and value > 0)):
        raise config.ConfigError(f'{where} role must be "admin", "maintain", "write" or a custom role\'s ID')
    if key in ("app", "team") and not (isinstance(value, str) and value):
        raise config.ConfigError(f"{where} {key} must be a slug")
    if key in ("org-admin", "deploy-keys") and value is not True:
        raise config.ConfigError(f"{where} {key} must be true")
    return actor | ({"mode": mode} if mode != "always" else {})


def _load_patterns(where: str, entry: dict[str, Any], key: str, target: str) -> list[str]:
    patterns = config.string_list(entry, key, [], where)
    other = next(prefix for t, prefix in TARGETS.items() if t != target)
    for pattern in patterns:
        if pattern.startswith(other) or (target == "tag" and pattern == "~DEFAULT_BRANCH"):
            raise config.ConfigError(f"{where} {key} names {pattern}, which isn't a {target}")
    return sorted({short_ref(p, target) for p in patterns})


def _load_ruleset(where: str, entry: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(entry) - set(KEYS))
    if unknown:
        raise config.ConfigError(f"{where} has unknown setting(s): {', '.join(unknown)}")
    ruleset: dict[str, Any] = {}
    target = entry.get("target", "branch")
    if target not in TARGETS:
        raise config.ConfigError(
            f"{where} target must be one of {', '.join(map(config.toml_string, TARGETS))} "
            "(repoutils doesn't manage push rulesets)"
        )
    if target != "branch":
        ruleset["target"] = target
    enforcement = entry.get("enforcement", "active")
    if enforcement not in ENFORCEMENTS:
        raise config.ConfigError(
            f"{where} enforcement must be one of {', '.join(map(config.toml_string, ENFORCEMENTS))}"
        )
    if enforcement != "active":
        ruleset["enforcement"] = enforcement
    for key in ("include", "exclude"):
        if patterns := _load_patterns(where, entry, key, target):
            ruleset[key] = patterns
    bypass = entry.get("bypass", [])
    if not isinstance(bypass, list):
        raise config.ConfigError(f"{where} bypass must be a list of tables")
    if bypass:
        ruleset["bypass"] = _normal([_load_actor(f"{where} bypass", actor) for actor in bypass])
    rules = entry.get("rules", {})
    if not isinstance(rules, dict):
        raise config.ConfigError(f"{where} rules must be a table")
    loaded = {}
    for key, value in rules.items():
        if "_" in key or _snake(key) not in RULES:
            raise config.ConfigError(f"{where} has an unknown rule {key} (rules are named like non-fast-forward)")
        if not isinstance(value, dict | bool):
            raise config.ConfigError(f"{where} rules.{key} must be true, false or a table")
        if _snake(key) not in ALLOWED_RULES[target]:
            other = "tag" if target == "branch" else "branch"
            raise config.ConfigError(
                f"{where} rules.{key} applies only to {other} rulesets, and this is a {target} one"
            )
        if value is not False:
            loaded[key] = _load_rule(f"{where} rules.{key}", _snake(key), {} if value is True else value)
    if loaded:
        ruleset["rules"] = loaded
    if "include" not in ruleset:
        example = '"~DEFAULT_BRANCH"' if target == "branch" else '"v*"'
        plural = "branches" if target == "branch" else "tags"
        raise config.ConfigError(f"{where} needs include, the {plural} it targets (e.g. [{example}])")
    return ruleset


def load_rulesets(root: Path = Path()) -> dict[str, dict[str, Any]]:
    """Each ruleset ``root/pyproject.toml`` defines, by name, with only its
    non-default settings, raising config.ConfigError (a ValueError) if one is malformed."""
    rulesets = {}
    for name, entry in config.section("rulesets", root).items():
        where = f"{TABLE[:-1]}.{_toml_key(name)}]"
        if not isinstance(entry, dict):
            raise config.ConfigError(f"{where} must be a table")
        rulesets[name] = _load_ruleset(where, entry)
    return rulesets


# Converting to and from GitHub's rulesets.


@dataclass
class Names:
    """The IDs of the GitHub Apps and teams a ruleset names, by slug."""

    apps: dict[str, int] = field(default_factory=dict)
    teams: dict[str, int] = field(default_factory=dict)

    @staticmethod
    def _slug(ids: dict[str, int], wanted: Any) -> str | None:
        return next((slug for slug, id_ in ids.items() if id_ == wanted), None)

    def app(self, app_id: Any) -> str | None:
        return self._slug(self.apps, app_id)

    def team(self, team_id: Any) -> str | None:
        return self._slug(self.teams, team_id)


def _actor_from_github(actor: dict[str, Any], names: Names) -> dict[str, Any]:
    kind, actor_id = actor.get("actor_type"), actor.get("actor_id")
    result: dict[str, Any]
    if kind == "RepositoryRole":
        result = {"role": next((name for name, id_ in ROLES.items() if id_ == actor_id), actor_id)}
    elif kind == "Integration":
        slug = names.app(actor_id)
        result = {"app": slug} if slug else {"app-id": actor_id}
    elif kind == "Team":
        slug = names.team(actor_id)
        result = {"team": slug} if slug else {"team-id": actor_id}
    elif kind == "OrganizationAdmin":
        result = {"org-admin": True}
    elif kind == "DeployKey":
        result = {"deploy-keys": True}
    else:
        result = {"actor-type": kind, "actor-id": actor_id}
    mode = _kebab(actor.get("bypass_mode") or "always")
    return result | ({"mode": mode} if mode != "always" else {})


def _check_from_github(check: dict[str, Any], names: Names) -> dict[str, Any]:
    result: dict[str, Any] = {"context": check["context"]}
    app_id = check.get("integration_id")
    if app_id is not None:
        slug = names.app(app_id)
        result |= {"app": slug} if slug else {"integration-id": app_id}
    return result


def from_github(ruleset: dict[str, Any], names: Names) -> dict[str, Any]:
    """GitHub's ruleset (as its API or a JSON export gives it) as load_rulesets gives it."""
    result: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        if ruleset.get(key, default) != default:
            result[key] = ruleset[key]
    target = result.get("target", "branch")
    ref_name = (ruleset.get("conditions") or {}).get("ref_name") or {}
    for key in ("include", "exclude"):
        patterns = sorted({short_ref(p, target) if target in TARGETS else p for p in ref_name.get(key) or []})
        if patterns:
            result[key] = patterns
    if "bypass_actors" not in ruleset:
        raise RuntimeError(
            f"GitHub didn't return the bypass list of ruleset {ruleset.get('name')!r}: "
            "it shows it only to a token that can administer the repository"
        )
    if ruleset["bypass_actors"]:
        result["bypass"] = _normal([_actor_from_github(actor, names) for actor in ruleset["bypass_actors"]])
    rules: dict[str, Any] = {}
    for rule in ruleset.get("rules") or []:
        rule_type, params = rule["type"], rule.get("parameters") or {}
        if rule_type not in RULES:
            # Shown as it is, though pyproject.toml can't give it, so check fails and dump warns.
            rules[_kebab(rule_type)] = _normal(_keys(params, _kebab))
            continue
        converted = {}
        for param, value in params.items():
            if param not in RULES[rule_type]:
                continue
            if param == "required_status_checks":
                value = [_check_from_github(check, names) for check in value or []]
            converted[_kebab(param)] = _normal(_keys(value, _kebab))
        rules[_kebab(rule_type)] = _compact(rule_type, converted)
    if rules:
        result["rules"] = rules
    return result


def _actor_to_github(actor: dict[str, Any], names: Names) -> dict[str, Any]:
    if "role" in actor:
        kind, actor_id = "RepositoryRole", ROLES.get(actor["role"], actor["role"])
    elif "app" in actor:
        kind, actor_id = "Integration", names.apps[actor["app"]]
    elif "team" in actor:
        kind, actor_id = "Team", names.teams[actor["team"]]
    elif "org-admin" in actor:
        kind, actor_id = "OrganizationAdmin", 1
    else:
        kind, actor_id = "DeployKey", None
    return {"actor_id": actor_id, "actor_type": kind, "bypass_mode": BYPASS_MODES[actor.get("mode", "always")]}


def to_github(name: str, ruleset: dict[str, Any], names: Names) -> dict[str, Any]:
    """The ruleset as GitHub's API (and a JSON import) takes it, with every parameter of each rule."""
    rules = []
    for key, params in ruleset.get("rules", {}).items():
        rule_type = _snake(key)
        full = {}
        for param, default in RULES[rule_type].items():
            if _kebab(param) not in params and (isinstance(default, Required) or param in OPTIONAL):
                continue
            value = params.get(_kebab(param), default)
            if param == "required_status_checks":
                value = [
                    {"context": c["context"]} | ({"integration_id": names.apps[c["app"]]} if "app" in c else {})
                    for c in value
                ]
            full[param] = _keys(value, _snake)
        rules.append({"type": rule_type} | ({"parameters": full} if RULES[rule_type] else {}))
    target = ruleset.get("target", "branch")
    return {
        "name": name,
        "target": target,
        "enforcement": ruleset.get("enforcement", "active"),
        "conditions": {
            "ref_name": {k: [_full_ref(p, target) for p in ruleset.get(k, [])] for k in ("include", "exclude")}
        },
        "bypass_actors": [_actor_to_github(actor, names) for actor in ruleset.get("bypass", [])],
        "rules": rules,
    }


def _differences(label: str, theirs: Any, ours: Any) -> list[str]:
    """How GitHub's value of label (theirs) differs from pyproject.toml's (ours): for
    lists, just the items one has and the other lacks."""
    if theirs == ours:
        return []
    if isinstance(theirs, list) and isinstance(ours, list):
        missing, extra = [v for v in ours if v not in theirs], [v for v in theirs if v not in ours]
        changes = [f"{label}: GitHub lacks:", *(f"  {_toml(v)}" for v in missing)] if missing else []
        return changes + ([f"{label}: GitHub also has:", *(f"  {_toml(v)}" for v in extra)] if extra else [])
    return [f"{label} is {_toml(theirs)} on GitHub, not {_toml(ours)}"]


def diff(theirs: dict[str, Any], ours: dict[str, Any]) -> list[str]:
    """How GitHub's ruleset (theirs) differs from pyproject.toml's (ours), both as load_rulesets gives them."""
    changes = []
    for key in KEYS[:-1]:
        default = DEFAULTS.get(key, [])
        changes += _differences(key, theirs.get(key, default), ours.get(key, default))
    their_rules, our_rules = theirs.get("rules", {}), ours.get("rules", {})
    for key in [*our_rules, *(k for k in their_rules if k not in our_rules)]:
        if key not in their_rules:
            changes.append(f"rule {key} is off on GitHub")
        elif key not in our_rules:
            changes.append(f"rule {key} is on on GitHub, but pyproject.toml leaves it off")
        else:
            schema = RULES.get(_snake(key), {})
            mine, yours = our_rules[key], their_rules[key]
            for param in [*mine, *(p for p in yours if p not in mine)]:
                fallback: Any = schema.get(_snake(param))
                fallback = None if fallback is None or isinstance(fallback, Required) else _normal(fallback)
                changes += _differences(f"rule {key} {param}", yours.get(param, fallback), mine.get(param, fallback))
    return changes


def _label(rule: str, param: str | None = None) -> str:
    """A comment line giving GitHub's ruleset page's label for a rule or parameter, if repoutils knows it."""
    label = LABELS.get(_snake(rule if param is None else f"{rule}.{param}"))
    return f"# {label}\n" if label else ""


def dump_toml(rulesets: Iterable[dict[str, Any]], names: Names) -> tuple[str, list[str]]:
    """GitHub's rulesets as TABLE's sub-tables, and a warning on each thing in them check would refuse."""
    entries, warnings = [], []
    for ruleset in rulesets:
        name, values = ruleset["name"], from_github(ruleset, names)
        header = f"{TABLE[:-1]}.{_toml_key(name)}"
        lines = [f"{header}]"] + [f"{key} = {_toml_block(values[key])}" for key in KEYS[:-1] if key in values]
        rules = values.get("rules", {})
        if any(not params for params in rules.values()):
            lines += ["", f"{header}.rules]"]
            lines += [f"{_label(key)}{key} = true" for key, params in rules.items() if not params]
        for key, params in rules.items():
            if params:
                lines += ["", f"{_label(key)}{header}.rules.{key}]"]
                lines += [f"{_label(key, p)}{p} = {_toml_block(v)}" for p, v in params.items()]
        entries.append("".join(f"{line}\n" for line in lines))
        checks = rules.get("required-status-checks", {}).get("required-status-checks", [])
        for entry in [*values.get("bypass", []), *checks]:
            for key in ("app-id", "integration-id", "team-id"):
                if key in entry:
                    kind = "team" if key == "team-id" else "app"
                    warnings.append(
                        f"{name}: couldn't find the slug of {kind} {entry[key]}, so gave its ID, which check "
                        f"refuses: replace it with {kind} = <its slug>"
                    )
            if "actor-type" in entry:
                warnings.append(f"{name}: repoutils doesn't know the bypass actor type {entry['actor-type']}")
        warnings += [f"{name}: repoutils doesn't know the rule {key}" for key in rules if _snake(key) not in RULES]
        target = values.get("target", "branch")
        if target not in TARGETS:
            warnings.append(f"{name}: repoutils doesn't manage {target} rulesets")
        else:
            other = [key for key in rules if _snake(key) in RULES and _snake(key) not in ALLOWED_RULES[target]]
            warnings += [f"{name}: check refuses rule {key} in a {target} ruleset" for key in other]
        if "include" not in values:
            warnings.append(f"{name}: it targets no {target}, so check refuses it without an include")
    return "\n".join(entries), list(dict.fromkeys(warnings))

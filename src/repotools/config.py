"""The project's ``[tool.repotools]`` table in ``pyproject.toml``.

Every setting has a default, so a project with no table (or no
``pyproject.toml``) still works; command-line options override both.
"""

import tomllib
from pathlib import Path
from typing import Any

PYPROJECT = "pyproject.toml"
DEFAULT_COMMENT_PREFIX = "repotools"


class ConfigError(ValueError):
    """``[tool.repotools]`` has a setting of the wrong type; the message says which."""


def load(root: Path = Path()) -> dict[str, Any]:
    """``[tool.repotools]`` from ``root/pyproject.toml``, or ``{}`` if either is missing."""
    path = root / PYPROJECT
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        table = tomllib.load(f).get("tool", {}).get("repotools", {})
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [tool.repotools] must be a table")
    return table


def section(name: str, root: Path = Path()) -> dict[str, Any]:
    """``[tool.repotools.<name>]``, or ``{}`` if it's missing."""
    table = load(root).get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"[tool.repotools.{name}] must be a table")
    return table


def string_list(table: dict[str, Any], key: str, default: list[str], where: str) -> list[str]:
    """``table[key]`` as a list of strings, or ``default`` if it's missing."""
    value = table.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} {key} must be a list of strings")
    return list(value)


def comment_prefix(root: Path = Path()) -> str:
    """The prefix of the hidden markers that find this project's PR comments
    (``comment-prefix``, default ``repotools``)."""
    value = load(root).get("comment-prefix", DEFAULT_COMMENT_PREFIX)
    if not isinstance(value, str) or not value or "-->" in value:
        raise ConfigError("[tool.repotools] comment-prefix must be a non-empty string without '-->'")
    return value


def marker(name: str, root: Path = Path()) -> str:
    """The hidden HTML comment that marks this project's ``name`` PR comment,
    e.g. ``<!-- repotools-patch-coverage -->``."""
    return f"<!-- {comment_prefix(root)}-{name} -->"


def default_branch(root: Path = Path()) -> str:
    """The branch to compare with when there's no PR base (``default-branch``, default ``main``)."""
    value = load(root).get("default-branch", "main")
    if not isinstance(value, str) or not value:
        raise ConfigError("[tool.repotools] default-branch must be a non-empty string")
    return value

"""The project's ``[tool.repoutils]`` table in ``pyproject.toml``.

Every setting has a default, so a project with no table (or no
``pyproject.toml``) still works; command-line options override both.
"""

import json
import math
import tomllib
from pathlib import Path
from typing import Any

PYPROJECT = "pyproject.toml"
DEFAULT_COMMENT_PREFIX = "repoutils"


class ConfigError(ValueError):
    """``[tool.repoutils]`` has a setting of the wrong type; the message says which."""


def load(root: Path = Path()) -> dict[str, Any]:
    """``[tool.repoutils]`` from ``root/pyproject.toml``, or ``{}`` if either is missing."""
    path = root / PYPROJECT
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        tool = tomllib.load(f).get("tool", {})
    if not isinstance(tool, dict):
        raise ConfigError(f"{path}: [tool] must be a table")
    table = tool.get("repoutils", {})
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: [tool.repoutils] must be a table")
    return table


def section(name: str, root: Path = Path()) -> dict[str, Any]:
    """``[tool.repoutils.<name>]``, or ``{}`` if it's missing."""
    table = load(root).get(name, {})
    if not isinstance(table, dict):
        raise ConfigError(f"[tool.repoutils.{name}] must be a table")
    return table


def string_list(table: dict[str, Any], key: str, default: list[str], where: str) -> list[str]:
    """``table[key]`` as a list of strings, or ``default`` if it's missing."""
    value = table.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} {key} must be a list of strings")
    return list(value)


def number(table: dict[str, Any], key: str, default: float, where: str) -> float:
    """``table[key]`` as a finite number that isn't negative, or ``default`` if it's missing."""
    value = table.get(key, default)
    # TOML has inf and nan, and no rate is ever over either, so they would turn a limit off unseen.
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ConfigError(f"{where} {key} must be a number, 0 or more")
    return value


def integer(table: dict[str, Any], key: str, default: int, where: str) -> int:
    """``table[key]`` as a whole number that isn't negative, or ``default`` if it's missing."""
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{where} {key} must be a whole number, 0 or more")
    return value


def optional_number(table: dict[str, Any], key: str, default: float, where: str) -> float | None:
    """As ``number``, but ``None`` if ``table[key]`` is ``false``, which turns the setting off."""
    if table.get(key) is False:
        return None
    try:
        return number(table, key, default, where)
    except ConfigError as error:
        raise ConfigError(f"{error}, or false") from None


def comment_prefix(root: Path = Path()) -> str:
    """The prefix of the hidden markers that find this project's PR comments
    (``comment-prefix``, default ``repoutils``)."""
    value = load(root).get("comment-prefix", DEFAULT_COMMENT_PREFIX)
    if not isinstance(value, str) or not value or "-->" in value:
        raise ConfigError("[tool.repoutils] comment-prefix must be a non-empty string without '-->'")
    return value


def marker(name: str, root: Path = Path()) -> str:
    """The hidden HTML comment that marks this project's ``name`` PR comment,
    e.g. ``<!-- repoutils-patch-coverage -->``."""
    return f"<!-- {comment_prefix(root)}-{name} -->"


def default_branch(root: Path = Path()) -> str:
    """The branch to compare with when there's no PR base (``default-branch``, default ``main``)."""
    value = load(root).get("default-branch", "main")
    if not isinstance(value, str) or not value:
        raise ConfigError("[tool.repoutils] default-branch must be a non-empty string")
    return value


def toml_string(value: str) -> str:
    """value as a TOML basic string, quoted and escaped, for output to paste into pyproject.toml."""
    # JSON's escapes are all valid TOML, but JSON leaves DEL unescaped, which TOML forbids.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_value(value: bool | str) -> str:
    """value as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return toml_string(value)

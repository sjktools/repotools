"""Check that uv.lock only has wheels for the platforms the project allows.

``[tool.repoutils.lock-platforms]`` lists them: ``supported`` (the platforms
``[tool.uv] environments`` is meant to allow) and ``tolerated`` (ones uv keeps
wheels for despite ``environments``, such as iOS and Emscripten). Each entry is
``os`` or ``os:arch``, with ``*`` for any arch, matched against a wheel's
platform tag split into its OS and arch (``manylinux_2_17_x86_64`` is
``manylinux:x86_64``; a name containing ``linux`` also matches ``linux``).
"""

import argparse
import dataclasses
import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from repoutils import config


# How _classify_platform treats an (OS, arch) pair, as split by _split_platform.
# SUPPORTED platforms are ones we intend to support, matching `[tool.uv]
# environments` in pyproject.toml; TOLERATED ones are platforms uv keeps wheels
# for even though `environments` excludes them. uv.lock may only have wheels for
# these two.
class PlatformClassification(StrEnum):
    SUPPORTED = "supported"
    TOLERATED = "tolerated"
    UNSUPPORTED = "unsupported"


DEFAULT_SUPPORTED = ("any", "linux:x86_64", "linux:aarch64", "macosx")
DEFAULT_TOLERATED = ("ios", "pyemscripten")


@dataclasses.dataclass(frozen=True)
class Platforms:
    """The ``os`` or ``os:arch`` patterns a project supports or tolerates."""

    supported: tuple[str, ...] = DEFAULT_SUPPORTED
    tolerated: tuple[str, ...] = DEFAULT_TOLERATED

    @classmethod
    def from_config(cls, root: Path = Path()) -> "Platforms":
        table = config.section("lock-platforms", root)
        where = "[tool.repoutils.lock-platforms]"
        return cls(
            tuple(config.string_list(table, "supported", list(DEFAULT_SUPPORTED), where)),
            tuple(config.string_list(table, "tolerated", list(DEFAULT_TOLERATED), where)),
        )


def _matches(pattern: str, os: str, arch: str | None) -> bool:
    """Whether ``os``/``arch`` matches ``pattern``: ``os`` alone allows any
    arch, ``os:arch`` only that one (``*`` for any). ``linux`` matches any OS
    name containing it (``manylinux``, ``musllinux``)."""
    want_os, _, want_arch = pattern.partition(":")
    if want_os != os and not (want_os == "linux" and "linux" in os):
        return False
    return want_arch in ("", "*") or want_arch == arch


def _classify_platform(os_arch: tuple[str, str | None], platforms: Platforms = Platforms()) -> PlatformClassification:
    os, arch = os_arch
    if any(_matches(pattern, os, arch) for pattern in platforms.supported):
        return PlatformClassification.SUPPORTED
    if any(_matches(pattern, os, arch) for pattern in platforms.tolerated):
        return PlatformClassification.TOLERATED
    return PlatformClassification.UNSUPPORTED


# An OS name, any version glued onto it (`manylinux2014`) or following it in
# `_<number>` parts (`macosx_10_15`), then the arch, which may itself contain
# `_` (`x86_64`) but never starts with a digit.
_PLATFORM_RE = re.compile(r"(?P<os>[a-z]+?)\d*(?:_\d+)*_(?P<arch>[a-z].*)")


def _split_platform(platform: str) -> tuple[str, str | None]:
    if platform == "any":
        return "any", None
    if platform == "win32":  # the one tag with no "_<arch>" suffix
        return "win", "x86"
    m = _PLATFORM_RE.fullmatch(platform)
    if m is None:
        raise ValueError(f"unrecognised wheel platform tag: {platform}")
    return m["os"], m["arch"]


def _wheel_platforms(filename: str) -> list[str]:
    # A wheel's platform tag is its last `-`-separated field, and may hold
    # several tags joined by `.` (e.g. `manylinux_2_17_x86_64.manylinux2014_x86_64`).
    return filename.removesuffix(".whl").split("-")[-1].split(".")


def _classify_tag(platform: str, platforms: Platforms = Platforms()) -> PlatformClassification:
    """How _classify_platform treats a wheel's platform tag; one it can't split is UNSUPPORTED."""
    try:
        return _classify_platform(_split_platform(platform), platforms)
    except ValueError:
        return PlatformClassification.UNSUPPORTED


def _wheel_filename(package_name: str, wheel: dict[str, Any]) -> str:
    # Registry wheels have a `url`; local ones a `path` or `filename`.
    location: str | None = wheel.get("url") or wheel.get("path") or wheel.get("filename")
    if location is None:
        raise ValueError(f"wheel in {package_name} has no url, path or filename")
    return location.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]


def _disallowed_wheels(lock: dict[str, Any], platforms: Platforms = Platforms()) -> list[tuple[str, str, str]]:
    """``(package, wheel filename, platform tag)`` for each of *lock*'s wheels for an UNSUPPORTED platform."""
    disallowed = []
    packages: list[dict[str, Any]] = lock.get("package", [])
    for package in packages:
        wheels: list[dict[str, Any]] = package.get("wheels", [])
        for wheel in wheels:
            filename = _wheel_filename(package["name"], wheel)
            for platform in _wheel_platforms(filename):
                if _classify_tag(platform, platforms) == PlatformClassification.UNSUPPORTED:
                    disallowed.append((package["name"], filename, platform))
    return disallowed


def main() -> int:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    disallowed = _disallowed_wheels(lock, Platforms.from_config())
    if disallowed:
        print("uv.lock has wheels for platforms that aren't allowed:")
        for name, filename, platform in disallowed:
            print(f"- {name}: {filename} ({platform})")
        print(
            "Check `[tool.uv] environments` in pyproject.toml. If the platform "
            "should be supported, add it there and to `supported` in "
            "[tool.repoutils.lock-platforms]; if uv keeps its wheels despite "
            "`environments`, add it to `tolerated` there."
        )
        return 1
    print("uv.lock only has wheels for allowed platforms.")
    return 0


def run(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="repoutils check-lock-platforms", description=__doc__.splitlines()[0]).parse_args(argv)
    return main()

# AGENTS.md

This file provides guidance to AI coding assistants working with code in this repository.

**`CLAUDE.md` is a symlink to this file**, not a separate file.
Always edit `AGENTS.md` directly.

This file and `README.md` are written one sentence per line, not wrapped at a fixed width, and `just sentence-per-line-check` enforces it.

## What this is

repoutils is a command-line tool, `repoutils <command>`, that collects the checks and CI helpers several projects used to keep in their own `scripts/` directories.
It's published to PyPI, and projects depend on it as a dev dependency.

It must stay standard library only, with no runtime dependencies: projects run it in CI jobs that hold write-capable tokens or check out untrusted Dependabot branches, installing it alone.
It supports Python 3.12 and later, so don't use newer syntax or `typing` features (e.g. `TypeIs`, or `except A, B:` without parentheses).

## Layout

- `src/repoutils/cli.py` is the entry point: `GROUPS` lists every command with its module and one-line summary, and `main()` dispatches to that module's `run(argv) -> int`.
- `src/repoutils/commands/<name>.py` is one module per command (`check-pins` is `check_pins.py`), which parses its own arguments with `argparse` (with `prog="repoutils <command>"`) in `run()`.
  Most also have a `main()` with plain parameters that the tests call directly.
- `src/repoutils/config.py` reads the project's `[tool.repoutils]` table from `pyproject.toml` in the current directory; every setting has a default, and command-line options override it.
- `src/repoutils/github.py` holds the GitHub API helpers the posting commands share: a request that never follows a redirect with the token to another host, paginated comment listing, `upsert_comment` and `hide_comments`.
- `tests/test_<module>.py` tests each module, and `tests/test_cli.py` runs every command through `cli.main()`.

To add a command, add its module with a `run()`, add it to `cli.GROUPS`, add its tests and a case in `tests/test_cli.py` if it needs one, and add it to the table in `README.md`.

A command that posts a PR comment finds its earlier comment by a hidden marker, `<!-- <comment-prefix>-<name> -->`, with `comment-prefix` from `[tool.repoutils]` (default `repoutils`), so projects that switch from their own scripts keep finding the comments those posted.

## Commands

CI (`.github/workflows/ci.yml`) delegates each step to the `justfile`; run `just ci` to run everything CI's test, build and coverage jobs run, or `just --list` for the recipes.
`just check` runs every check without fixing anything; `just fix` reformats and applies lint autofixes.
`just test` runs the tests, and `just coverage` runs them with a coverage report and writes `coverage.xml` and `junit.xml`.
Before opening a PR, run `just patch-coverage` too, which reports the changed lines the tests miss, and cover them where a test is worth it.

The justfile runs repoutils' own checks (`whitespace-check`, `check-pins` and so on) from this checkout, so CI dogfoods them.
Recipes that CI runs with a write-capable token or on a Dependabot branch use the justfile's `repoutils` variable, which runs `python -m repoutils` from `src/` with `uv run --no-project`, installing nothing (not even the project, whose build backend would otherwise run alongside the token).

`just mypy` type-checks `src` and `tests` in strict mode, so all new code, tests included, needs full type annotations.
`[tool.pytest.ini_options]` sets `filterwarnings = ["error"]`, so a warning fails its test: fix its cause, or assert it with `pytest.warns` around the call that raises it.

CI pins its tools: every `setup-uv` step sets `version` to the minimum in `pyproject.toml`'s `[tool.uv] required-version`, and every `setup-just` step sets `just-version` to the justfile's `minimum-version`; `just check-pins` fails if they differ.
`.github/workflows/actions.lock` pins every action the workflows use to a commit; after changing any `uses:`, regenerate it with `gh actions-lock` (the [github/gh-actions-lock](https://github.com/github/gh-actions-lock) extension), and never edit it by hand.
CI's `actions-lock` job verifies it.

## CI and releases

`ci.yml` runs the tests on Linux and macOS for each supported Python version, then `just check`, the build and a smoke test of the wheel, coverage (uploaded to Codecov, with the `CODECOV_TOKEN` secret) and, on PRs from this repository, posts the patch coverage and long files reports as PR comments.
Windows isn't supported for development, as `[tool.uv] environments` leaves it out of `uv.lock`; repoutils itself is pure Python.
On Dependabot PRs, its `dependabot-fix` job re-locks with our uv and syncs the dependency minimums, pushing with the `DEPENDABOT_FIX_TOKEN` secret (a Dependabot secret with contents write), so the push triggers CI.

Releases are cut from `main`: the Bump Version workflow opens a PR bumping `version` in `pyproject.toml`; once it merges, `ci.yml`'s `release` job tags `v<version>` and creates a GitHub release (with the `GH_ACTIONS_TOKEN` secret, so the tag push triggers the Publish workflow).
`publish-pypi.yml` then builds once, publishes to TestPyPI, checks the release installs from there, and only then publishes the same files to PyPI, with PEP 740 attestations, through trusted publishing (no API token), and checks it installs from PyPI.
Commenting just `/testpypi` on a PR from this repository publishes a dev build of its branch to TestPyPI only.

`claude.yml` and `claude-code-review.yml` run Claude Code when a comment starts with `@claude` (with the `CLAUDE_CODE_OAUTH_TOKEN` secret); their `route` jobs run `repoutils claude-route` from a sparse checkout of `src/repoutils/` with the runner's `python3`, installing nothing.
Commenting just `/autofix` on a PR runs `just fix` on its branch and pushes the result (with the `AUTOFIX_TOKEN` secret).

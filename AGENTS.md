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
- `src/repoutils/markdown.py` holds the Markdown reading that `check-sentence-per-line` and `check-prose` share.
  It skips code fences and splits a document's prose into sentences.
- `tests/test_<module>.py` tests each module, and `tests/test_cli.py` runs every command through `cli.main()`.

To add a command, add its module with a `run()`, add it to `cli.GROUPS`, add its tests and a case in `tests/test_cli.py` if it needs one, and add it to the table in `README.md`.

A command that posts a PR comment finds its earlier comment by a hidden marker, `<!-- <comment-prefix>-<name> -->`, with `comment-prefix` from `[tool.repoutils]` (default `repoutils`), so projects that switch from their own scripts keep finding the comments those posted.

## Commands

CI (`.github/workflows/ci.yml`) delegates each step to the `justfile`; run `just ci` to run everything CI's test, build and coverage jobs run, or `just --list` for the recipes.
`just check` runs every check except `lock-check` (which `just ci` runs first) without fixing anything; `just fix` reformats and applies lint autofixes.
`just test` runs the tests, and `just coverage` runs them with a coverage report and writes `coverage.xml` and `junit.xml`.
Before opening a PR, run `just patch-coverage` too, which reports the changed lines the tests miss, and cover them where a test is worth it.

The justfile runs repoutils' own checks (`whitespace-check`, `check-pins` and so on) from this checkout, so CI dogfoods them.
Recipes that CI runs with a write-capable token or on a Dependabot branch use the justfile's `repoutils` variable, which runs `python -m repoutils` from `src/` with `uv run --no-project`, installing nothing (not even the project, whose build backend would otherwise run alongside the token).
`just prose-check` runs `check-prose` with `--exit-zero`, so it reports where this file's or `README.md`'s prose is over a limit, without failing.
Its limits are ceilings, not targets, so reword a sentence that it lists, and don't split one that it didn't list.
Drop `--exit-zero` once both files pass.

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
The repository owner, or an organization member, commenting just `/testpypi` on an open PR from this repository publishes a dev build of its branch to TestPyPI only.

`claude.yml` and `claude-code-review.yml` run Claude Code when a comment starts with `@claude` (with the `CLAUDE_CODE_OAUTH_TOKEN` secret); their `route` jobs run `repoutils claude-route` from a sparse checkout of `src/repoutils/` with the runner's `python3`, installing nothing.
The repository owner, or an organization member, commenting just `/autofix` on an open PR from this repository runs `just fix` on its branch and pushes the result (with the `AUTOFIX_TOKEN` secret).

## Issue labels

`pyproject.toml`'s `[[tool.repoutils.labels]]` defines the labels, as exolot and taxtracker use them; `repoutils labels check` reports any the repository lacks, and `repoutils labels ensure` creates them.
Issues are labelled `dimension:value`.
When you file an issue, give it one `type:` and one `priority:` label, a `size:` label unless it's a parent issue, and any `concern:` labels that apply.

- `type:` (exactly one): `bug` (something behaves wrongly), `feature` (a new capability, such as a new command), `improvement` (makes something that already works better), `maintenance` (refactors, dead code, CI, dependencies; no visible change) or `docs` (documentation only).
- `priority:` (exactly one), defined by what to do about it: `p0` (drop everything: a release is broken, or a check passes or fails wrongly), `p1` (do next), `p2` (worth doing when convenient) or `p3` (someday/maybe).
- `size:` (exactly one, except on parent issues), by how many PRs it takes rather than hours: `s` (one small PR), `m` (one PR that needs some design) or `l` (several PRs).
  An `l` issue should become a parent issue with sub-issues; parent issues get no `size:` label, their sub-issues do.
- `concern:` (zero or more): `security` (a security risk or hardening) or `breaking` (changes commands, options, config or exit codes that projects rely on).

`question` is for issues that ask something rather than report or request something.
`dependencies` marks Dependabot's PRs, which also get `type:maintenance` (`.github/dependabot.yml` sets both).
Close duplicates and things that won't be done with GitHub's "duplicate" and "not planned" close reasons; there are no labels for them.
Use only these labels: applying a label that doesn't exist creates a new grey one, so a typo or a new value silently adds to the scheme.

# AGENTS.md

This file provides guidance to AI coding assistants working with code in this repository.

**`CLAUDE.md` is a symlink to this file**, not a separate file.
Always edit `AGENTS.md` directly.

This file and `README.md` are written one sentence per line, not wrapped at a fixed width, and `just sentence-per-line-check` enforces it.

## What this is

repoutils is a command-line tool, run as `repoutils <command>`.
It collects the checks and CI helpers that several projects used to keep in their own `scripts/` directories.
It's published to PyPI, and projects depend on it as a dev dependency.

It must stay standard library only, with no runtime dependencies.
Projects install it alone in CI jobs that hold write-capable tokens or check out untrusted Dependabot branches.

It supports Python 3.12 and later, so don't use newer syntax or `typing` features.
Two examples are `TypeIs`, and `except A, B:` without parentheses.

## Layout

- `src/repoutils/cli.py` is the entry point.
  `GROUPS` lists every command with its module and one-line summary.
  `main()` dispatches to that module's `run(argv) -> int`.
- `src/repoutils/commands/<name>.py` holds one command per module, so `check-pins` is `check_pins.py`.
  Each parses its own arguments in `run()` with `argparse`, using `prog="repoutils <command>"`.
  Most also have a `main()` with plain parameters that the tests call directly.
- `src/repoutils/config.py` reads the project's `[tool.repoutils]` table from `pyproject.toml` in the current directory.
  Every setting has a default, and command-line options override it.
- `src/repoutils/github.py` holds the GitHub API helpers that the posting commands share.
  They are a request function, paginated comment listing, `upsert_comment` and `hide_comments`.
  The request function never follows a redirect to another host with the token.
- `src/repoutils/markdown.py` holds the Markdown reading that `check-sentence-per-line` and `check-prose` share.
  It skips code fences and splits a document's prose into sentences.
- `tests/test_<module>.py` tests each module.
  `tests/test_cli.py` runs every command through `cli.main()`.

To add a command:

1. Add its module, with a `run()`.
2. Add it to `cli.GROUPS`.
3. Add its tests, and a case in `tests/test_cli.py` if it needs one.
4. Add it to the table in `README.md`.

A command that posts a PR comment finds its earlier comment by a hidden marker, `<!-- <comment-prefix>-<name> -->`.
`comment-prefix` comes from `[tool.repoutils]` and defaults to `repoutils`.
A project that switches from its own scripts sets it, to keep finding the comments those scripts posted.

## Commands

CI (`.github/workflows/ci.yml`) delegates each step to the `justfile`.

- `just ci` runs everything that CI's test, build and coverage jobs run.
- `just --list` shows the recipes.
- `just check` runs every check without fixing anything.
  It leaves out `lock-check`, which `just ci` runs first.
- `just fix` reformats and applies lint autofixes.
- `just test` runs the tests.
- `just coverage` runs them with a coverage report and writes `coverage.xml` and `junit.xml`.
- `just patch-coverage` reports the changed lines that the tests miss.
  Run it before opening a PR, and cover those lines where a test is worth it.

The justfile runs repoutils' own checks from this checkout, such as `whitespace-check` and `check-pins`, so CI dogfoods them.
Some recipes run in CI with a write-capable token or on a Dependabot branch.
Those use the justfile's `repoutils` variable, which runs `python -m repoutils` from `src/` with `uv run --no-project`.
That installs nothing, not even the project, whose build backend would otherwise run alongside the token.
`just prose-check` runs `check-prose`, which fails if the prose in this file or `README.md` is over a limit.
Its limits are ceilings, not targets, so reword a sentence that it lists, and don't split one that it didn't list.
A file made of very short sentences fails too, and reads no better than one made of long ones.

`just mypy` type-checks `src` and `tests` in strict mode.
All new code needs full type annotations, tests included.
`[tool.pytest.ini_options]` sets `filterwarnings = ["error"]`, so a warning fails its test.
Fix the warning's cause, or assert it with `pytest.warns` around the call that raises it.

CI pins its tools.
Every `setup-uv` step sets `version` to the minimum in `pyproject.toml`'s `[tool.uv] required-version`.
Every `setup-just` step sets `just-version` to the justfile's `minimum-version`.
`just check-pins` fails if they differ.

`.github/workflows/actions.lock` pins every action the workflows use to a commit.
After changing any `uses:`, regenerate it with `gh actions-lock`, the [github/gh-actions-lock](https://github.com/github/gh-actions-lock) extension, and never edit it by hand.
CI's `actions-lock` job verifies it.

## CI and releases

`ci.yml` runs these, in order:

1. The tests, on Linux and macOS for each supported Python version.
2. `just check`.
3. The build, and a smoke test of the wheel.
4. Coverage, which it uploads to Codecov with the `CODECOV_TOKEN` secret.
5. On PRs from this repository, the patch coverage and long files reports, which it posts as PR comments.

Windows isn't supported for development, because `[tool.uv] environments` leaves it out of `uv.lock`.
repoutils itself is pure Python.

On Dependabot PRs, the `dependabot-fix` job in `ci.yml` re-locks with our uv and syncs the dependency minimums.
It pushes with the `DEPENDABOT_FIX_TOKEN` secret, so that the push triggers CI.
That is a Dependabot secret with contents write.

Releases are cut from `main`.
The Bump Version workflow opens a PR that bumps `version` in `pyproject.toml`.
Once it merges, the `release` job in `ci.yml` tags `v<version>` and creates a GitHub release.
It uses the `GH_ACTIONS_TOKEN` secret, so that the tag push triggers the Publish workflow.

`publish-pypi.yml` then does this:

1. It builds once.
2. It publishes to TestPyPI.
3. It checks that the release installs from TestPyPI.
4. Only then does it publish the same files to PyPI, and check that the release installs from there.

It publishes with PEP 740 attestations, through trusted publishing, so there is no API token.

Two comments act on an open PR from this repository.
Only the repository owner or an organization member can use them, and the comment must be just the command.

- `/testpypi` publishes a dev build of the PR's branch to TestPyPI only.
- `/autofix` runs `just fix` on the PR's branch and pushes the result, with the `AUTOFIX_TOKEN` secret.

`claude.yml` and `claude-code-review.yml` run Claude Code, with the `CLAUDE_CODE_OAUTH_TOKEN` secret, when a comment starts with `@claude`.
Their `route` jobs run `repoutils claude-route` from a sparse checkout of `src/repoutils/`.
Those jobs use the runner's `python3` and install nothing.

## Issue labels

`pyproject.toml`'s `[[tool.repoutils.labels]]` defines the labels, as exolot and taxtracker use them.
`repoutils labels check` reports any that the repository lacks, and `repoutils labels ensure` creates them.

Issues are labelled `dimension:value`.
When you file an issue, give it:

- one `type:` label
- one `priority:` label
- a `size:` label, unless it's a parent issue
- any `concern:` labels that apply

`type:` (exactly one):

- `bug`: something behaves wrongly.
- `feature`: a new capability, such as a new command.
- `improvement`: makes something that already works better.
- `maintenance`: refactors, dead code, CI or dependencies, with no visible change.
- `docs`: documentation only.

`priority:` (exactly one), defined by what to do about it:

- `p0`: drop everything.
  A release is broken, or a check passes or fails wrongly.
- `p1`: do next.
- `p2`: worth doing when convenient.
- `p3`: someday/maybe.

`size:` (exactly one, except on parent issues), by how many PRs it takes rather than hours:

- `s`: one small PR.
- `m`: one PR that needs some design.
- `l`: several PRs.

An `l` issue should become a parent issue with sub-issues.
Parent issues get no `size:` label, but their sub-issues do.

`concern:` (zero or more):

- `security`: a security risk or hardening.
- `breaking`: changes commands, options, config or exit codes that projects rely on.

`question` is for issues that ask something rather than report or request something.
`dependencies` marks Dependabot's PRs, which also get `type:maintenance`, and `.github/dependabot.yml` sets both.

Close duplicates with GitHub's "duplicate" close reason, and things that won't be done with "not planned", as there are no labels for either.

Use only these labels, because applying one that doesn't exist creates a new grey label, and a typo or a new value then silently adds to the scheme.

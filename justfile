set minimum-version := '1.58.0'

# Runs repoutils from this checkout's source with nothing installed (it needs
# only the standard library), for recipes CI runs with a write-capable token or
# on an untrusted Dependabot branch: `uv run` without --no-project would build
# and install the project, running its build backend, alongside the token.
repoutils := "PYTHONPATH=src uv run --no-project python -m repoutils"

# Dummy assignment, so every recipe run sets up the repo's local Git config
# (see `repoutils git-config-setup`). `eager` makes just evaluate it even though
# nothing uses it, whether or not `set lazy` is on (casey/just#3759).
eager _ := shell(repoutils + ' git-config-setup')

# List available recipes
default:
    @just --list

# Check that uv.lock is up to date with pyproject.toml
lock-check:
    uv lock --check

# Sync dependencies (including dev group)
sync:
    uv sync --group dev

# Run the test suite
test:
    uv run --group dev pytest -q

# Run the test suite with coverage, printing a report and writing coverage.xml and junit.xml
coverage *args:
    uv run --group dev pytest -q --cov --cov-report=term-missing --cov-report=xml --junitxml=junit.xml -o junit_family=legacy {{ args }}

# Run coverage, then report coverage of the lines changed since base
patch-coverage base='origin/main': coverage (diff-cover base)

# Report coverage of the lines changed since base from an existing coverage.xml (extra args go to diff-cover)
diff-cover base='origin/main' *args:
    uv run --group dev diff-cover coverage.xml --compare-branch={{ base }} --branch-coverage {{ args }}

# The posting recipes run with a write-capable token in CI, so they use
# the repoutils variable above, installing nothing.

# Post a patch coverage report (default patch-coverage.md) to a PR as a comment, or update the existing one
[positional-arguments]
patch-coverage-post *args:
    {{ repoutils }} post-patch-coverage "$@"

# Wait (up to 10 minutes) for Codecov to have a PR base commit's coverage: arguments are owner/repo and the base SHA
[positional-arguments]
wait-for-base-coverage *args:
    {{ repoutils }} wait-for-base-coverage --check-name coverage "$@"

# List files over the line limit as Markdown, or post that to a PR (--post <PR>)
[positional-arguments]
long-files *args:
    {{ repoutils }} long-files "$@"

# Build the sdist and wheel into dist/
build:
    uv build

# The publish jobs run this between attest-action and uv publish, which would
# otherwise upload a file without its attestation. It installs nothing, as
# they run it with id-token: write.

# Check that every distribution in dist/ (or the directory given) has a PEP 740 publish attestation
[positional-arguments]
check-attestations dist="dist":
    {{ repoutils }} check-attestations "$1"

# Install the wheel `just build` built into a fresh environment and check that it runs
smoke:
    #!/usr/bin/env bash
    set -euo pipefail
    version="$(uv version --short)"
    wheel="dist/repoutils-${version}-py3-none-any.whl"
    run() { uvx --isolated --no-cache --from "$wheel" repoutils "$@"; }
    actual="$(run --version)"
    if [ "$actual" != "repoutils ${version}" ]; then
        echo "expected 'repoutils ${version}' from ${wheel} --version, got '${actual}'" >&2
        exit 1
    fi
    run --help >/dev/null
    run check-whitespace >/dev/null
    echo "${wheel} installs and runs"

# Retries while the index catches up with a new upload.
# Check that a released version installs from PyPI (or the index given) and runs
[positional-arguments]
check-release version index="https://pypi.org/simple/":
    #!/usr/bin/env bash
    set -euo pipefail
    for delay in 0 10 20 40 80 160; do
        sleep "$delay"
        if actual="$(uvx --isolated --no-cache --default-index "$2" --from "repoutils==$1" repoutils --version)" && [ "$actual" = "repoutils $1" ]; then
            echo "repoutils $1 installs from $2 and runs"
            exit 0
        fi
    done
    echo "repoutils $1 did not install from $2 and run" >&2
    exit 1

# Lint with ruff
lint:
    uv run --group dev ruff check .

# Lint with ruff, applying its autofixes
lint-fix:
    uv run --group dev ruff check --fix .

# Format code with ruff
fmt:
    uv run --group dev ruff format .

# Check code is formatted, without changing it
fmt-check:
    uv run --group dev ruff format --check .

# Format justfile using just's built-in formatter
just-fmt:
    {{ quote(just_executable()) }} --justfile {{ quote(justfile()) }} --fmt

# Check that the justfile is formatted according to just's built-in formatter
just-fmt-check:
    {{ quote(just_executable()) }} --justfile {{ quote(justfile()) }} --fmt --check

# Type-check src and tests with mypy (strict; see pyproject.toml's [tool.mypy])
mypy:
    uv run --group dev mypy

# Lint the GitHub Actions workflows with jactionlint
lint-workflows:
    uv run --group dev jactionlint

# repoutils' own checks, run from this checkout, so CI dogfoods them.

# Check Git-tracked text files for trailing whitespace and bad newlines
whitespace-check:
    uv run repoutils check-whitespace

# Check AGENTS.md and README.md stay one sentence per line
sentence-per-line-check:
    uv run repoutils check-sentence-per-line

# Check AGENTS.md and README.md prose for long, clause-heavy sentences, or too many short ones
prose-check:
    uv run repoutils check-prose

# Check that every workflow and local action pins the uv version pyproject.toml requires and the just version the justfile requires
check-pins:
    uv run repoutils check-pins

# Check that every actions/checkout step sets persist-credentials, so none keeps its token by default
check-checkout-credentials:
    uv run repoutils check-checkout-credentials

# Check that the workflows use exactly the secrets pyproject.toml lists
secrets-workflows-check:
    uv run repoutils secrets check-workflows

# Check that uv.lock only has wheels for supported OS/CPU combinations
lock-platforms-check:
    uv run repoutils check-lock-platforms

# Check that dependency minimums in pyproject.toml match uv.lock
requirements-check:
    {{ repoutils }} requirements-min-versions check

# Rewrite dependency minimums in pyproject.toml to the versions in uv.lock
requirements-fix:
    {{ repoutils }} requirements-min-versions fix

# Re-lock a Dependabot branch's uv.lock with this uv, from the base ref's uv.lock
dependabot-relock base:
    {{ repoutils }} dependabot-relock {{ base }}

# Run all checks, without fixing anything or running tests
check: lint fmt-check just-fmt-check mypy lint-workflows whitespace-check sentence-per-line-check prose-check check-pins check-checkout-credentials secrets-workflows-check lock-platforms-check requirements-check

# Formatting first fixes the long lines it can, so lint-fix stops only on
# findings that need a hand edit.

# Reformat code and autofix lint issues
fix: fmt just-fmt lint-fix

# Run everything CI's build, test and coverage jobs run (except the PR-only patch coverage), on this machine's Python and OS
ci: lock-check sync check coverage build smoke

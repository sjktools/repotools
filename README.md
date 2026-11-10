# repotools

Repository tooling for Python projects that use uv, just and GitHub Actions, run as `repotools <command>`.

It collects the checks and CI helpers that used to live in the `scripts/` directories of [taxtracker](https://github.com/skissane/taxtracker) and [exolot](https://github.com/skissane/exolot), so each project can depend on one package instead of keeping its own copies.

repotools needs only the Python standard library (3.12 or later), and has no dependencies.
That's deliberate: projects run it in CI jobs that hold write-capable tokens or check out untrusted Dependabot branches, where nothing else should be installed.

## Installing

Add it to its own dependency group, and include that group in `dev`:

```toml
[dependency-groups]
repotools = ["repotools>=0.1.0"]
dev = [{ include-group = "repotools" }, "pytest>=9"]
```

Then run a command with `uv run repotools <command>`.
In a job with a write-capable token, use `uv run --only-group repotools repotools <command>`, which installs only repotools (hash-checked against `uv.lock`) and not the project or its other dependencies.

## Commands

Run `repotools --help` for the list, and `repotools <command> --help` for a command's options.

| Command | What it does |
| --- | --- |
| `check-whitespace` | Fails on trailing whitespace, a leading blank line or a missing or extra final newline in any Git-tracked text file. |
| `check-sentence-per-line` | Fails if a Markdown file has two sentences on one line. |
| `check-blame-ignore-revs` | Fails unless `.git-blame-ignore-revs` lists only full hashes of commits that are ancestors of a target. |
| `check-screenshots-dir` | Fails unless the screenshots directory's `.gitignore` is the only file tracked there, and still ignores everything else. |
| `git-config-setup` | Sets the repo's local `blame.ignoreRevsFile`; idempotent, for a justfile to run on every recipe. |
| `long-files` | Lists the files over a line limit as Markdown, changed files first, or posts that to a PR. |
| `check-pins` | Fails unless every `setup-uv` and `setup-just` step pins the versions `pyproject.toml` and the justfile require. |
| `check-checkout-credentials` | Fails on any `actions/checkout` step that doesn't set `persist-credentials`. |
| `claude-route` | Prints `review`, `other` or `none` for a comment on stdin, for routing between the Claude workflows. |
| `requirements-min-versions` | Checks or fixes `pyproject.toml`'s `>=` minimums against the versions in `uv.lock`. |
| `dependabot-relock` | Re-locks a Dependabot branch's `uv.lock` with this uv, to exactly Dependabot's versions. |
| `check-lock-platforms` | Fails if `uv.lock` has wheels for platforms the project doesn't allow. |
| `post-comment` | Posts a Markdown file to a PR, editing the earlier comment or hiding it with `--hide-previous`. |
| `post-patch-coverage` | Posts diff-cover's Markdown patch coverage report to a PR, reformatted. |
| `wait-for-base-coverage` | Waits for Codecov to have a PR base commit's coverage, so the PR's comparison works. |
| `post-screenshots` | Uploads a directory of PNGs and posts them to a PR as a comment. |
| `recreate-pr` | Recreates a PR as a draft from the same branch and closes the original. |
| `check-attestations` | Fails unless every distribution in `dist/` has a PEP 740 publish attestation. |

The commands that post to a PR read `$GITHUB_TOKEN`, falling back to `gh auth token`.

## Configuration

Every setting is optional, and lives in the project's `pyproject.toml`:

```toml
[tool.repotools]
# Prefix of the hidden markers that find this project's PR comments,
# e.g. <!-- repotools-patch-coverage -->.
comment-prefix = "repotools"
# Compared with when there's no PR base.
default-branch = "main"

[tool.repotools.sentence-per-line]
files = ["AGENTS.md", "README.md"]

[tool.repotools.screenshots]
dir = "screenshots"

[tool.repotools.lock-platforms]
# "os" allows any arch; "os:arch" one ("*" for any). "linux" matches manylinux and musllinux.
supported = ["any", "linux:x86_64", "linux:aarch64", "macosx"]
tolerated = ["ios", "pyemscripten"]

[tool.repotools.long-files]
include = ["*.py"]
exclude = ["*/migrations/*"]
limit = 500
limits = { "tests/*" = 700 }
policy = "Split a long file only where that makes the code better."
```

## Development

See [AGENTS.md](AGENTS.md).

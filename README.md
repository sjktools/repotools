# repoutils

Repository tooling for Python projects that use uv, just and GitHub Actions, run as `repoutils <command>`.

It collects the checks and CI helpers that used to live in the `scripts/` directories of [taxtracker](https://github.com/skissane/taxtracker) and [exolot](https://github.com/skissane/exolot), so each project can depend on one package instead of keeping its own copies.

repoutils needs only the Python standard library (3.12 or later), and has no dependencies.
That's deliberate: projects run it in CI jobs that hold write-capable tokens or check out untrusted Dependabot branches, where nothing else should be installed.

## Installing

Add it to its own dependency group, and include that group in `dev`:

```toml
[dependency-groups]
repoutils = ["repoutils>=0.1.0"]
dev = [{ include-group = "repoutils" }, "pytest>=9"]
```

Then run a command with `uv run repoutils <command>`.
In a job with a write-capable token, use `uv run --only-group repoutils repoutils <command>`, which installs only repoutils (hash-checked against `uv.lock`) and not the project or its other dependencies.

## Commands

Run `repoutils --help` for the list, and `repoutils <command> --help` for a command's options.

| Command | What it does |
| --- | --- |
| `check-whitespace` | Fails on trailing whitespace, a leading blank line or a missing or extra final newline in any Git-tracked text file. |
| `check-sentence-per-line` | Fails if a configured Markdown file (by default `AGENTS.md` and `README.md`) has two sentences on one line. |
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
| `labels` | `labels check` fails if the repository lacks a label `pyproject.toml` defines; `labels ensure` creates it. |
| `repo-settings` | `repo-settings check` fails if a repository setting differs from `pyproject.toml`; `repo-settings ensure` changes it. |
| `check-attestations` | Fails unless every distribution in `dist/` has a PEP 740 publish attestation. |

The commands that post to a PR, `labels` and `repo-settings` read `$GITHUB_TOKEN`, falling back to `gh auth token`.

## Configuration

Every setting is optional, and lives in the project's `pyproject.toml`:

```toml
[tool.repoutils]
# Prefix of the hidden markers that find this project's PR comments,
# e.g. <!-- repoutils-patch-coverage -->.
comment-prefix = "repoutils"
# Compared with when there's no PR base.
default-branch = "main"

[tool.repoutils.sentence-per-line]
files = ["AGENTS.md", "README.md"]

[tool.repoutils.screenshots]
dir = "screenshots"

[tool.repoutils.lock-platforms]
# "os" allows any arch; "os:arch" one ("*" for any). "linux" matches manylinux and musllinux.
supported = ["any", "linux:x86_64", "linux:aarch64", "macosx"]
tolerated = ["ios", "pyemscripten"]

[tool.repoutils.long-files]
include = ["*.py"]
exclude = ["*/migrations/*"]
limit = 500
limits = { "tests/*" = 700 }
policy = "Split a long file only where that makes the code better."

# The labels `repoutils labels` checks or creates: color and description are optional.
[[tool.repoutils.labels]]
name = "bug"
color = "d73a4a"
description = "Something isn't working"

[[tool.repoutils.labels]]
name = "dependencies"

# The repository settings `repoutils repo-settings` checks or changes; see below.
[tool.repoutils.repo-settings]
description = "Repository tooling for Python projects"
homepage = "https://pypi.org/project/repoutils/"
issues = true
wiki = false
pull-request-creation = "collaborators"
squash-merge = { allow = true, title = "pr-title", message = "pr-body" }
merge-commit.allow = false
rebase-merge.allow = false
delete-branch-on-merge = true
```

### Labels

`repoutils labels ensure` creates each missing label (grey, if `pyproject.toml` gives it no color), and fills in a description that's empty on GitHub with the one `pyproject.toml` gives.
It leaves a name whose case differs, a different color or a different non-empty description as it is, and reports it, unless you pass `--strict`, which changes them to match.
It never changes or deletes a label `pyproject.toml` doesn't define.
If a request fails part way, the labels before it are already changed, and running `ensure` again finishes the job.

`repoutils labels check` changes nothing, and fails exactly when `ensure`, with the same `--strict`, would change something; it reports the differences `ensure` would leave alone without failing.
With `--exit-zero` it reports the same, but exits 0 unless it couldn't check (for example on a bad token), which `|| true` would hide.

In GitHub Actions, `ensure` needs `issues: write` permission, and `check` needs `issues: read` on a private repository.

### Repository settings

`[tool.repoutils.repo-settings]` covers the settings on a repository's General settings page that most projects want to keep the same, mainly its Features and Pull Requests sections.
Every one is optional, though the command fails if the table sets none, and `repoutils repo-settings` leaves a setting the table leaves out as it is on GitHub.

| Setting | Values | On GitHub |
|---|---|---|
| `description`, `homepage` | a string (`""` for none) | The repository's About box. |
| `issues`, `projects`, `wiki`, `discussions`, `downloads`, `pull-requests` | `true` or `false` | Whether each feature is on. |
| `pull-request-creation` | `"all"` or `"collaborators"` | Who can open pull requests. |
| `merge-commit.allow`, `squash-merge.allow`, `rebase-merge.allow` | `true` or `false` | Which merge methods are allowed. |
| `merge-commit.title` | `"merge-message"` or `"pr-title"` | The default title of a merge commit (`"merge-message"` is "Merge pull request #123 from branch"). |
| `merge-commit.message` | `"pr-title"`, `"pr-body"` or `"blank"` | The default message of a merge commit. |
| `squash-merge.title` | `"pr-title"` or `"commit-or-pr-title"` | The default title of a squash commit (`"commit-or-pr-title"` uses the commit's title if there's only one). |
| `squash-merge.message` | `"pr-body"`, `"commit-messages"` or `"blank"` | The default message of a squash commit. |
| `allow-auto-merge` | `true` or `false` | Allow auto-merge. |
| `allow-update-branch` | `true` or `false` | Always suggest updating pull request branches. |
| `delete-branch-on-merge` | `true` or `false` | Automatically delete head branches. |
| `require-web-commit-signoff` | `true` or `false` | Require contributors to sign off on web-based commits. |

A commit message is sent with its title, so setting only `squash-merge.message` (or `merge-commit.message`) keeps the title GitHub has; GitHub refuses some title and message pairs, which `ensure` reports as an HTTP 422 error.

It deliberately can't rename, transfer or archive the repository, change its default branch or visibility, or make it a template, and rejects a table that tries.

`repoutils repo-settings ensure` changes every setting that differs, in one request, and fails if GitHub doesn't apply one (it ignores some changes, such as one an organization's policy forbids, rather than refusing them).
`repoutils repo-settings check` changes nothing, and fails exactly when `ensure` would change something; `--exit-zero` works as it does for `labels check`.

`ensure` needs a token that can administer the repository (a fine-grained token with Administration write permission), which GitHub Actions' `GITHUB_TOKEN` can't be given.

## Development

See [AGENTS.md](AGENTS.md).

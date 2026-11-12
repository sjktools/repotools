# repoutils

repoutils is a set of repository checks and CI helpers for Python projects that use uv, just and GitHub Actions.
You run it as `repoutils <command>`.

I used to keep these checks and helpers in the `scripts/` directories of several personal projects, such as [exolot](https://github.com/skissane/exolot).
I pulled them together here so that each project can depend on one package instead of keeping its own copies.

repoutils has no dependencies, and needs only the Python standard library (3.12 or later).
It should stay that way, because projects run it in CI jobs that hold write-capable tokens or check out untrusted Dependabot branches, and nothing else should be installed in those jobs.

## Installing

Give repoutils its own dependency group, and include that group in `dev`:

```toml
[dependency-groups]
repoutils = ["repoutils>=0.1.0"]
dev = [{ include-group = "repoutils" }, "pytest>=9"]
```

Then run a command with `uv run repoutils <command>`.
In a job with a write-capable token, use `uv run --only-group repoutils repoutils <command>` instead.
That installs only repoutils, hash-checked against `uv.lock`, and not the project or its other dependencies.

## Commands

`repoutils --help` lists the commands, and `repoutils <command> --help` shows a command's options.

| Command | What it does |
| --- | --- |
| `check-whitespace` | Fails on trailing whitespace, a leading blank line or a missing or extra final newline in any Git-tracked text file. |
| `check-sentence-per-line` | Fails if a configured Markdown file (by default `AGENTS.md` and `README.md`) has two sentences on one line. |
| `check-prose` | Fails if a configured Markdown file's prose has long, clause-heavy sentences, or too many short ones. |
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
| `labels` | Checks or creates the labels `pyproject.toml` defines, or prints the ones the repository has. |
| `repo-settings` | Checks or changes the repository settings `pyproject.toml` gives, or prints the ones the repository has. |
| `check-attestations` | Fails unless every distribution in `dist/` has a PEP 740 publish attestation. |

The commands that post to a PR need a GitHub token, and so do `labels` and `repo-settings`.
They read `$GITHUB_TOKEN`, and fall back to `gh auth token`.

## Configuration

Settings go in the project's `pyproject.toml`, and all of them are optional:

```toml
[tool.repoutils]
# Prefix of the hidden markers used to find this project's PR comments,
# e.g. <!-- repoutils-patch-coverage -->.
comment-prefix = "repoutils"
# The branch to compare with when there's no PR base.
default-branch = "main"

[tool.repoutils.sentence-per-line]
files = ["AGENTS.md", "README.md"]

# The limits `repoutils check-prose` holds these files to; see below.
[tool.repoutils.prose]
files = ["AGENTS.md", "README.md"]
max-words-per-sentence = 18

[tool.repoutils.screenshots]
dir = "screenshots"

[tool.repoutils.lock-platforms]
# "os" allows any arch, and "os:arch" allows one arch ("*" for any).
# "linux" matches manylinux and musllinux.
supported = ["any", "linux:x86_64", "linux:aarch64", "macosx"]
tolerated = ["ios", "pyemscripten"]

[tool.repoutils.long-files]
include = ["*.py"]
exclude = ["*/migrations/*"]
limit = 500
limits = { "tests/*" = 700 }
policy = "Split a long file only where that makes the code better."

# The labels that `repoutils labels` checks or creates; color and description are optional.
[[tool.repoutils.labels]]
name = "bug"
color = "d73a4a"
description = "Something isn't working"

[[tool.repoutils.labels]]
name = "dependencies"

# The repository settings that `repoutils repo-settings` checks or changes (see below).
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

### Prose

`repoutils check-prose` measures the prose in each configured Markdown file, and fails a file whose sentences are, on the whole, long and full of joined clauses.
That style is common in machine-written documents, and it's tiring to read.

The opposite fault fails too.
Prose that has been cut up only to get under the limits ends up as a run of very short sentences, which reads no better.
So one limit caps how many of those a file can have.
That limit leaves out list items, where a terse sentence is at home.
It also leaves out a sentence that ends in a colon, which leads into a list or a code block.
Treat the limits as ceilings and not as targets, and leave a sentence alone if the file already passes.

The command skips code blocks, headings, tables, block quotes, comments and any line that starts with an HTML block tag.
A block quote runs on to the next blank line, list item or heading.
It counts inline code as one word.
Text with no closing punctuation, or with fewer than three words, isn't counted, such as a terse bullet.

It reads sentences as English punctuates them.
A sentence ends at a full stop, a question mark or an exclamation mark, and the next one starts with a capital, a digit or inline code.
A colon ends one too, at the end of a paragraph or a list item.
A new line after one of those marks starts a sentence too, whatever its first word is.
So in a wrapped paragraph, a sentence that starts mid-line with a lowercase word, such as "uv", is read as part of the one before it.

| Setting | Default | What it limits |
|---|---|---|
| `max-words-per-sentence` | 18 | The mean number of words in a sentence. |
| `max-long-sentences` | 12 | The percentage of sentences with more than `long-sentence-words` words (default 30). |
| `max-short-sentences` | 25 | The percentage of sentences with fewer than `short-sentence-words` words (default 8), list items and lead-ins aside. |
| `max-commas-per-sentence` | 1.3 | The mean number of commas in a sentence. |
| `max-semicolons-per-sentence` | 0.05 | The mean number of semicolons in a sentence. |
| `max-joints-per-sentence` | 0.6 | The mean number of clause joints in a sentence. |

A clause joint is a semicolon, a colon, a dash or a comma before a word such as "and", "but", "because" or "which".
Set a limit to `false` to turn it off.
A file with fewer than `min-sentences` sentences (default 15) is too short to judge, and passes.
`min-sentences`, `long-sentence-words` and `short-sentence-words` are whole numbers.

The limits apply to a whole file's rates and not to each sentence, so a file with a few long sentences can still pass.
Under each limit that a file is over, the command lists the five sentences that add most to it.
The two limits on long sentences share one list, under the first of them that the file is over.
`--stats` prints every rate for every file, and `--exit-zero` reports the same but exits 0.

The defaults for long sentences come from comparing READMEs written before language models with recent machine-written documents.
That sample was small (17 documents), and the short-sentence limit rests on less still (a few rewrites of this README), so adjust the limits to suit your project.

### Labels

`repoutils labels ensure` creates each label the repository lacks, grey if `pyproject.toml` gives it no color.
Where a label's description is empty on GitHub, `ensure` fills it in with the one from `pyproject.toml`.

Other differences are reported but left alone: a name in a different case, a different color, or a different description that isn't empty.
Pass `--strict` to have those changed to match as well.
`ensure` never changes or deletes a label `pyproject.toml` doesn't define.
If a request fails part way through, the labels before it have already been changed, so run `ensure` again to finish the job.

`repoutils labels check` changes nothing.
It fails in exactly the cases where `ensure`, given the same `--strict`, would change something.
The differences `ensure` would leave alone are reported too, but they don't make it fail.

With `--fail-on-unexpected`, `check` also lists the labels the repository has but `pyproject.toml` doesn't define (matching names regardless of case), and fails if there are any.
Use it when you want `pyproject.toml` to be the complete list of labels.
`ensure` still leaves those labels alone, so you have to delete them on GitHub or add them to `pyproject.toml`.

With `--exit-zero`, `check` reports the same things but exits 0, unless it couldn't do the check at all (because of a bad token, for example).
That's the difference from `|| true`, which would hide that kind of failure as well.

In GitHub Actions, `ensure` needs the `issues: write` permission, and `check` needs `issues: read` if the repository is private.

`repoutils labels dump` prints every label the repository has as `[[tool.repoutils.labels]]` entries, each with its name, its color and its description if it has one.
You can paste them into `pyproject.toml` and edit them from there.
If you paste them in unchanged, `check --strict` passes.
`dump` neither reads nor changes `pyproject.toml`.

### Repository settings

`[tool.repoutils.repo-settings]` covers the settings on a repository's General settings page that most projects want to keep the same, mainly the ones in its Features and Pull Requests sections.
All of them are optional, but `check` and `ensure` fail if the table doesn't set any.
A setting the table leaves out stays as it is on GitHub.

| Setting | Values | On GitHub |
|---|---|---|
| `description`, `homepage` | a string (`""` for none) | The repository's About box. |
| `issues`, `projects`, `wiki`, `discussions`, `pull-requests` | `true` or `false` | Whether each feature is on. |
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

GitHub only accepts a commit message along with its title.
So when you set only `squash-merge.message` (or `merge-commit.message`), repoutils sends the title GitHub already has.
GitHub refuses some pairs of title and message, and `ensure` reports that as an HTTP 422 error.

Some things are left out on purpose.
`repo-settings` can't rename, transfer or archive the repository, change its default branch or visibility, or make it a template, and it rejects a table that tries to.

`repoutils repo-settings ensure` changes every setting that differs, all in one request.
It fails if GitHub doesn't apply one of them.
That can happen because GitHub ignores some changes rather than refusing them, such as a change an organization's policy forbids.

`repoutils repo-settings check` changes nothing, and fails in exactly the cases where `ensure` would change something.
`--exit-zero` works the same way as it does for `labels check`.

`ensure` needs a token that can administer the repository, such as a fine-grained token with the Administration write permission.
The `GITHUB_TOKEN` that GitHub Actions provides can't be given that permission.

`repoutils repo-settings dump` prints every setting in the table above with the value the repository has for it.
The output is a `[tool.repoutils.repo-settings]` table with a sub-table for each merge method.
Paste it into `pyproject.toml` and trim it down to the settings you want to keep the same.
If you paste it in unchanged, `check` passes.
`dump` neither reads nor changes `pyproject.toml`.
If GitHub doesn't return a setting, or returns a value the table can't express, `dump` leaves it out and says so on stderr.

## Development

See [AGENTS.md](AGENTS.md).

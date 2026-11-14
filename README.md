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
| `added-strings` | Lists the lines a PR adds that have strings `pyproject.toml` configures, such as `# noqa:`, or posts that to a PR, for information only. |
| `post-patch-coverage` | Posts diff-cover's Markdown patch coverage report to a PR, reformatted, with the project's coverage and its change from the PR's base, and warns when either falls short. |
| `post-coverage-status` | Posts a commit's project and patch coverage as commit statuses, as a record and not a gate. |
| `wait-for-base-coverage` | Waits for a PR's base commit to have its `coverage/project` status, so the PR's comparison works. |
| `post-screenshots` | Uploads a directory of PNGs and posts them to a PR as a comment. |
| `recreate-pr` | Recreates a PR as a draft from the same branch and closes the original. |
| `labels` | Checks or creates the labels `pyproject.toml` defines, or prints the ones the repository has. |
| `repo-settings` | Checks or changes the repository settings `pyproject.toml` gives, or prints the ones the repository has. |
| `rulesets` | Checks, creates or updates the branch and tag rulesets `pyproject.toml` defines, or prints the ones the repository has. |
| `environments` | Checks, creates or updates the deployment environments `pyproject.toml` defines, or prints the ones the repository has. |
| `actions-settings` | Checks or changes the Actions settings `pyproject.toml` gives, or prints the ones the repository has. |
| `secrets` | Checks the repository has the Actions, Dependabot and environment secrets `pyproject.toml` names, or prints the names it has. Also checks the workflows use exactly those secrets. |
| `deploy-keys` | Checks or creates the deploy keys `pyproject.toml` declares, and the secrets that hold their private halves, or prints the keys the repository has. |
| `security-settings` | Checks or changes the security settings `pyproject.toml` gives, such as secret protection, or prints the ones the repository has. |
| `check-attestations` | Fails unless every distribution in `dist/` has a PEP 740 publish attestation. |

The commands that post to a PR need a GitHub token, and so do `labels`, `repo-settings`, `actions-settings`, `rulesets`, `environments`, `secrets`, `deploy-keys` and `security-settings`.
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

# The names of the commit statuses `repoutils post-coverage-status` posts, and
# when `repoutils post-patch-coverage` warns (false for never); see below.
[tool.repoutils.coverage]
project-status = "coverage/project"
patch-status = "coverage/patch"
warn-project-drop = 2
warn-patch-below = 1

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

# The strings `repoutils added-strings` points out in the lines a PR adds; see below.
[tool.repoutils.added-strings]
exclude = ["pyproject.toml", "*.md"]

[[tool.repoutils.added-strings.strings]]
string = "TODO"

[[tool.repoutils.added-strings.strings]]
regex = '^\s*if TYPE_CHECKING\b'
name = "if TYPE_CHECKING:"

[[tool.repoutils.added-strings.strings]]
regex = "# type: ?ignore"
message = "please fix the type error instead, if you can"
include = ["*.py"]

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

# The Actions settings that `repoutils actions-settings` checks or changes (see below).
[tool.repoutils.actions-settings]
allowed-actions = "all"
require-sha-pinning = true
default-token-permissions = "read"
oidc.immutable-subject = true

# A ruleset `repoutils rulesets` checks or creates, giving only what isn't a default; see below.
[tool.repoutils.rulesets.main]
include = ["~DEFAULT_BRANCH"]
bypass = [{ role = "admin", mode = "pull-request" }]

[tool.repoutils.rulesets.main.rules]
deletion = true
non-fast-forward = true
pull-request = { allowed-merge-methods = ["squash"] }
required-status-checks = { required-status-checks = [{ context = "test", app = "github-actions" }] }

# The names of the secrets `repoutils secrets` checks the repository has; see below.
[tool.repoutils.secrets]
actions = []
dependabot = ["DEPENDABOT_FIX_TOKEN"]
environments.release = ["RELEASE_TOKEN"]
other = ["ORG_DEPLOY_KEY"]

# The security settings that `repoutils security-settings` checks or changes; see below.
[tool.repoutils.security-settings]
dependabot-alerts = true
secret-protection = true
push-protection = true

# The deployment environments `repoutils environments` checks or creates; see below.
[tool.repoutils.environments.pypi]
allowed-tags = ["v*"]

[tool.repoutils.environments.testpypi]

# A deploy key `repoutils deploy-keys` checks or creates, and the secret that holds its private half; see below.
[tool.repoutils.deploy-keys.release]
write = true
secret = { environment = "release", name = "RELEASE_DEPLOY_KEY" }
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
It counts inline code as one word, and a URL as one word.
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

A file that can't be read, such as one that isn't valid UTF-8, is reported on one line as `<file>: cannot read (<reason>)`.
The command still checks the other files and then exits 1, even with `--exit-zero`, and `check-sentence-per-line` treats such a file the same way.

The defaults for long sentences come from comparing READMEs written before language models with recent machine-written documents.
That sample was small (17 documents), and the short-sentence limit rests on less still (a few rewrites of this README), so adjust the limits to suit your project.

### Added strings

`repoutils added-strings` finds the lines the working tree adds since the merge base of `--base` and `HEAD`, and lists the ones that have any of the configured strings.
Each entry under `strings` gives either `string`, a plain substring, or `regex`, a Python regular expression searched for in each line.
Its `message` defaults to asking the author to double check that the string is actually necessary.
A regex can tell code from a string that names it, which a plain substring can't, so tests and docs that mention it don't trip it.
For example, `^\s*if TYPE_CHECKING\b` matches only the statement, and repoutils' own `pyproject.toml` has one for a `# noqa` comment.
A regex's `name` is what the report calls its matches in place of the regex.

`include` and `exclude` are glob patterns for the paths to search, which a `*` matches across `/`.
The ones in the table apply to every string, and an entry's own narrow its string further.
The project's own `pyproject.toml` and docs often name the strings, so leave them out.

The report is for information only, so the command exits 0 whatever it finds.
It still fails if it can't check, such as on a bad setting, a failed `git` command or a failed request to GitHub.
With `--post <PR>`, it posts the report only when the PR adds a string.
If a later push removes them all, it updates the earlier comment to say so, rather than leaving a stale warning.

### Coverage status

`repoutils post-coverage-status <sha>` posts a commit's coverage as two statuses on it, named `coverage/project` and `coverage/patch`.
The project status comes from `coverage.xml`, and covers the whole project.
The patch status comes from diff-cover's JSON report (`--patch-report`), and covers the lines that a PR, or a commit outside one, changes.

Post them on each commit of the default branch, and on each PR's head, wherever the token can.
The default branch's project statuses then become a history of its coverage.
Given `--base`, the PR's base or the commit's parent, the project status also shows the change from that commit's project status.
If that commit has none yet, the status leaves the change out.

Both are always `success`, because they record coverage and don't judge it.
Their descriptions are the figures in one fixed format, so other tools can parse them:

```text
96.52% (2114/2172 lines, 610/650 branches), +0.12 points from 1a2b3c4
92.30% (12/13 lines)
```

The project figure is coverage.py's total, which counts covered lines and branches together.
It's cut to two decimal places rather than rounded, so it shows 100.00% only when nothing is missed.
Its branches part appears only when the report measured branches.
The change is the difference between the two figures as shown.
The patch figure counts lines as diff-cover does, and reads `no changed lines to cover` when there are none.

A token that can post statuses can post one under any name, including a required check's.
So post from a job of its own that installs nothing, and hand it the reports as an artifact.
That job needs `statuses: write`, which a fork's or Dependabot's PR doesn't get.
`--dry-run` prints the statuses without posting them.

### Patch coverage comment

`repoutils post-patch-coverage <PR>` posts diff-cover's Markdown report to the PR, and edits that comment on later runs.
Given `--coverage-report coverage.xml`, the comment also shows the project's coverage.
Given `--base` too, the PR's base commit, it shows that commit's `coverage/project` status and the change from it.
It never compares with an older commit, whose figure would include whatever else has merged since.
When the base has no status, the comment says so and shows the PR's figure alone.

The comment warns, and prints a `::warning::` for the run page, in two cases:

- Project coverage fell from the base's by more than `warn-project-drop` points (default 2).
- Patch coverage is more than `warn-patch-below` points under the base's project coverage (default 1), or the PR's when the base has none.

These only warn, so coverage never fails the command.
It still fails when it can't produce the report, such as on a bad report, a bad setting or a failed request to GitHub.
`--output <file>` appends the comment to a file, such as `$GITHUB_STEP_SUMMARY`, instead of posting it.
That shows the same figures on fork and Dependabot PRs, whose token can't comment.

The base's CI may still be running when the PR's coverage is ready, so run `repoutils wait-for-base-coverage <owner/repo> <base>` first.
It polls the base's `coverage/project` status, and stops as soon as it's there.
While it's missing, it watches the base's `post-coverage-status` check run (`--check-name`), and stops once that finishes.
After 10 minutes (`--timeout`) it stops anyway.
Giving up only warns and exits 0, so a broken base doesn't fail the PR's own checks.
It needs `checks: read` and `statuses: read`.

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

### Actions settings

`[tool.repoutils.actions-settings]` covers the repository's Actions settings page (Settings, Actions, General).
It works like `[tool.repoutils.repo-settings]`.
All of its settings are optional, but `check` and `ensure` fail if the table doesn't set any.
A setting the table leaves out stays as it is on GitHub.

| Setting | Values | On GitHub |
|---|---|---|
| `allowed-actions` | `"all"`, `"owner"`, `"selected"` or `"disabled"` | Which actions and reusable workflows may run: all, only the owner's, the ones `selected-actions` gives, or none (Actions is off). |
| `require-sha-pinning` | `true` or `false` | Require actions to be pinned to a full-length commit SHA. |
| `selected-actions.github-owned` | `true` or `false` | Allow actions created by GitHub. |
| `selected-actions.verified-creators` | `true` or `false` | Allow actions by Marketplace verified creators. |
| `selected-actions.patterns` | a list of strings | The other actions and reusable workflows allowed, such as `"astral-sh/*"`, in any order. |
| `default-token-permissions` | `"read"` or `"write"` | The `GITHUB_TOKEN`'s default permissions: read only, or read and write. |
| `can-approve-pull-requests` | `true` or `false` | Allow GitHub Actions to create and approve pull requests. |
| `fork-pr-approval` | `"first-time-contributors-new-to-github"`, `"first-time-contributors"` or `"all-external-contributors"` | Which outside contributors need approval before their fork's pull request runs workflows. |
| `retention-days` | a positive integer | How many days checks, workflow runs, commit statuses, artifacts and logs are kept. |
| `cache-retention-days` | a positive integer | How many days an unused cache is kept. |
| `cache-size-gb` | a positive integer | How many gigabytes of caches the repository keeps before evicting the oldest. |
| `oidc.use-default-subject` | `true` or `false` | Use the default template for the OIDC token's subject claim. |
| `oidc.include-claim-keys` | a list of strings | The claims a custom subject template joins, in order, which needs `oidc.use-default-subject = false`. |
| `oidc.immutable-subject` | `true` or `false` | Use the immutable subject claim, based on the repository's ID rather than its name. |

The `selected-actions` settings need `allowed-actions = "selected"`, as GitHub only has them then.

`repoutils actions-settings ensure` changes every setting that differs, with one request for each part of the page that needs one.
It fails if GitHub doesn't apply one of them.
An organization's policy can lock some settings, such as the `GITHUB_TOKEN`'s write permission.
GitHub refuses a change to a locked setting with an HTTP 409 error, and `ensure` reports it.
Some settings don't apply to every repository, such as fork pull request approval on a private one.
GitHub answers those with HTTP 404, so `check` and `ensure` fail if the table sets one, and `dump` leaves it out.

`repoutils actions-settings check` changes nothing, and fails in exactly the cases where `ensure` would change something.
`--exit-zero` works the same way as it does for `labels check`.

`check` and `dump` need a token with read access to the repository's administration settings, and to its Actions for the `oidc` settings.
For a fine-grained token, that's the Administration and Actions read permissions, and `ensure` needs both as write permissions.
The `GITHUB_TOKEN` that GitHub Actions provides can't be given the Administration permission.

`repoutils actions-settings dump` prints every setting in the table above with the value the repository has for it.
It leaves out `selected-actions` unless `allowed-actions` is `"selected"`, and `oidc.include-claim-keys` with the default template.
Paste the output into `pyproject.toml` and trim it down to the settings you want to keep the same.
If you paste it in unchanged, `check` passes.
`dump` neither reads nor changes `pyproject.toml`.
If GitHub doesn't return a setting, or returns a value the table can't express, `dump` leaves it out and says so on stderr.

### Rulesets

Each `[tool.repoutils.rulesets.<name>]` table is one of the repository's branch or tag rulesets (Settings, Rules, Rulesets).
It gives only what isn't a new ruleset's default.
Anything it leaves out is off or empty, so the table is the whole ruleset.
`ensure` turns off any rule the table leaves out.

Push rulesets are left alone.
So are rulesets the repository inherits from its organization, and legacy branch protection rules.

| Setting | Values | On GitHub |
|---|---|---|
| `target` | `"branch"` (the default) or `"tag"` | Whether it's a branch or a tag ruleset. |
| `enforcement` | `"active"` (the default), `"evaluate"` or `"disabled"` | Whether the ruleset is enforced. |
| `include`, `exclude` | branch or tag name patterns, such as `["main", "release/*"]` or `["v*"]`, or `"~ALL"` (or `"~DEFAULT_BRANCH"`, for a branch ruleset) | The branches or tags it targets. `refs/heads/` or `refs/tags/` is optional. `include` is required. |
| `bypass` | a list of tables (see below) | Who can bypass the ruleset. |
| `rules.<rule>` | `true`, or a table of the rule's parameters | Each rule that's on. |

Each `bypass` entry names one actor.
That's `role` (`"admin"`, `"maintain"`, `"write"` or a custom role's ID), `app` (a GitHub App's slug), `team` (a team's slug), `org-admin = true` or `deploy-keys = true`.
It can also give a `mode`: `"always"` (the default), `"pull-request"` or `"exempt"`.

For example, these two tag rulesets stop anyone updating or deleting a `v*` tag, and let only deploy keys create one.
They're separate because a bypass applies to every rule in its ruleset.

```toml
[tool.repoutils.rulesets.release-tags-immutable]
target = "tag"
include = ["v*"]

[tool.repoutils.rulesets.release-tags-immutable.rules]
update = true
deletion = true

[tool.repoutils.rulesets.release-tags-create]
target = "tag"
include = ["v*"]
bypass = [{ deploy-keys = true }]

[tool.repoutils.rulesets.release-tags-create.rules]
creation = true
```

Rules and their parameters have the names that GitHub's REST API and a ruleset's JSON export use, in kebab case.
So `non_fast_forward` is `rules.non-fast-forward`.
A tag ruleset can have only the rules GitHub's ruleset page offers for tags.
Those are `creation`, `update`, `deletion`, `required-linear-history`, `required-deployments`, `required-signatures`, `required-status-checks`, `non-fast-forward`, `tag-name-pattern` and the three commit metadata patterns.
`tag-name-pattern` is for tag rulesets only, and `check` refuses a rule in the wrong kind of ruleset.
A parameter left out has the default that GitHub's ruleset page gives a new rule, which is off, 0 or empty.
The exceptions are `pull-request.allowed-merge-methods`, which allows all three methods, and `merge-queue`, which takes the page's values.

Above each rule and parameter whose label on GitHub's ruleset page repoutils knows, `dump` writes that label as a comment.
These are the names that differ most from their labels:

| In `pyproject.toml` | On GitHub's ruleset page |
|---|---|
| `rules.deletion` | Restrict deletions |
| `rules.non-fast-forward` | Block force pushes |
| `required-status-checks.strict-required-status-checks-policy` | Require branches to be up to date before merging |
| `required-status-checks.do-not-enforce-on-create` | Do not require status checks on creation |
| `pull-request.dismiss-stale-reviews-on-push` | Dismiss stale pull request approvals when new commits are pushed |
| `pull-request.require-last-push-approval` | Require approval of the most recent reviewable push |
| `pull-request.required-review-thread-resolution` | Require conversation resolution before merging |
| `merge-queue.max-entries-to-build` | Build concurrency |
| `merge-queue.grouping-strategy` | Require all queue entries to pass required checks (`"ALLGREEN"`) or not (`"HEADGREEN"`) |

A required status check is `{ context = "test", app = "github-actions" }`.
`app` is the slug of the GitHub App that must report the check.
Leave it out to accept the check from any source.
`check` and `ensure` look up the app's ID themselves, and refuse an ID in `pyproject.toml`.

`repoutils rulesets ensure` creates each missing ruleset.
It replaces each one that differs with the table's, sending every parameter of each rule.
Then it fails if GitHub didn't apply a change.
It ignores a rule parameter repoutils doesn't know, so `ensure` may reset one.
It never deletes a ruleset.

`repoutils rulesets check` changes nothing, and fails in exactly the cases where `ensure` would change something.
`--exit-zero` and `--fail-on-unexpected` work as they do for `labels check`.
Here `--fail-on-unexpected` fails on branch and tag rulesets that `pyproject.toml` doesn't define.

All three need a token that can administer the repository, because GitHub shows a ruleset's bypass list only to such a token.
For a fine-grained token, that's Administration write for `ensure`, and read for `check` and `dump`.
GitHub Actions' `GITHUB_TOKEN` can't be given it.
If a bypass list names a team, the token also needs the organization's Members read permission, to look the team up.

`repoutils rulesets dump` prints every branch and tag ruleset the repository has as these tables.
You can paste them into `pyproject.toml`.
If you paste them in unchanged, `check --fail-on-unexpected` passes, unless `dump` warned about something `check` refuses.
With `--from-file`, it prints the rulesets in JSON files exported from GitHub's ruleset page instead.
That lets you design a ruleset in GitHub's UI, on any repository, and convert it.
`dump` neither reads nor changes `pyproject.toml`.

GitHub has no API that gives a GitHub App's slug from its ID.
So `dump` collects the slugs of the apps that ran checks on the latest commit of the default branch, and of each branch the rulesets name.
If it can't find an app's or a team's slug, it prints the ID and warns.
Replace that ID with the slug before you run `check`.

### Environments

Each `[tool.repoutils.environments.<name>]` table is one of the repository's deployment environments, which a GitHub Actions job names with `environment:`.
An empty table only asks for the environment to exist.
A setting the table leaves out stays as it is on GitHub.
Environment names match regardless of case, as on GitHub, which can't rename one.

| Setting | Values | On GitHub |
|---|---|---|
| `deployment-branches` | `"all"`, `"protected"` or `"selected"` | Which branches and tags can deploy: any, protected branches only, or selected ones. |
| `allowed-branches`, `allowed-tags` | name patterns, such as `["v*"]` | The selected branches and tags that can deploy. Each list is the complete one, and giving either implies `"selected"`. |
| `wait-timer` | minutes, from 0 to 43200 | How long a job waits before it deploys. |
| `reviewers` | up to six users (`"login"`) or the owner's teams (`"owner/team-slug"`) | Required reviewers, one of whom must approve a deployment. |
| `prevent-self-review` | `true` or `false` | Stop whoever started a run from approving it. |
| `can-admins-bypass` | `true` or `false` | Allow administrators to bypass these protection rules. |

Wait timers and required reviewers need a public repository or a paid plan.
Otherwise GitHub refuses them, and `ensure` reports an HTTP error.

`repoutils environments ensure` creates each missing environment and changes every setting that differs.
It adds and deletes allowed branches and tags to match their lists.
Then it fails if GitHub didn't apply a change.
It never deletes an environment, and never changes its secrets, variables or custom deployment protection rules.

`repoutils environments check` changes nothing, and fails in exactly the cases where `ensure` would change something.
`--exit-zero` and `--fail-on-unexpected` work as they do for `labels check`.

`ensure` needs a token that can administer the repository, such as a fine-grained token with the Administration write permission.
The `GITHUB_TOKEN` that GitHub Actions provides can't be given that permission.

`repoutils environments dump` prints every environment the repository has, with all of its settings.
It gives the allowed branches and tags only for `"selected"`.
Paste the output into `pyproject.toml` and trim it down to the settings you want to keep the same.
If you paste it in unchanged, `check --fail-on-unexpected` passes.
`dump` neither reads nor changes `pyproject.toml`.

### Secrets

`[tool.repoutils.secrets]` names the secrets a repository's workflows need.
`actions` lists its Actions secrets.
`dependabot` lists its Dependabot secrets, the ones a workflow that Dependabot triggers can read.
`environments.<name>` lists the secrets of the deployment environment called `<name>`.
A list the table leaves out isn't checked.
Names match regardless of case, as on GitHub.
`other` lists the secrets workflows use that the repository doesn't hold, such as an organization's.
`check` and `dump` ignore `other`, and only `check-workflows` reads it.

`repoutils secrets` only ever reads secrets' names.
GitHub's API never returns a value, so there's no `ensure`.
Set a missing secret with `gh secret set NAME`, or on the repository's settings page.
Add `--app dependabot` for a Dependabot secret, or `--env ENVIRONMENT` for an environment's.

`repoutils secrets check` fails if the repository lacks a secret the table names, or one of its environments.
It doesn't create a missing environment, which `repoutils environments ensure` can do.
`--exit-zero` and `--fail-on-unexpected` work as they do for `labels check`.
Here `--fail-on-unexpected` fails on secrets of a listed kind that the table doesn't name.
So `dependabot = []` means there must be no Dependabot secrets.
Once the table has `environments`, that covers the secrets of every environment, including those it doesn't name.
So `environments = {}` means no environment may have secrets.

`check` and `dump` need a token that can list the repository's secrets.
For a fine-grained token, that's the Secrets and Dependabot secrets read permissions.
Environment secrets need the Actions and Environments read permissions too.
GitHub Actions' `GITHUB_TOKEN` can't be given them.

`repoutils secrets dump` prints the name of every secret the repository has, as `[tool.repoutils.secrets]`.
It gives both repository lists, and a list for each environment that has secrets.
You can paste it into `pyproject.toml`.
If you paste it in unchanged, `check --fail-on-unexpected` passes.
`dump` neither reads nor changes `pyproject.toml`.

`repoutils secrets check-workflows` needs no token, so it can run with a project's other checks.
It reads every workflow in `.github/workflows` for the secrets it uses, as `secrets.NAME` or `secrets['NAME']`.
It fails if a workflow uses a secret that none of the lists names.
It also fails if a list names a secret that no workflow uses.
So the table can't fall behind the workflows.
A job gets an environment's secrets only if it names that environment as its `environment`.
So `check-workflows` also fails if a job uses a secret that only environments hold, without naming one of them.
It can't tell a job's environment when an expression gives it, so it doesn't check that job.
It skips `GITHUB_TOKEN`, which GitHub provides, and anything in a comment.

### Deploy keys

Each `[tool.repoutils.deploy-keys.<title>]` table is one of the repository's deploy keys, which the table's key names by its title.
`write = true` gives the key write access, and leaving it out makes it read-only, as on GitHub.
`secret` names the Actions secret that holds the key's private half, as `{ name = "NAME" }` for a repository secret.
For an environment's secret, add `environment = "ENVIRONMENT"`.
The public key isn't in `pyproject.toml`, so replacing a key needs no commit.
An empty `[tool.repoutils.deploy-keys]` table declares that the repository has no keys with write access.

`repoutils deploy-keys ensure` creates each missing key that has a `secret`.
It generates an Ed25519 key pair with `ssh-keygen`, in a private temporary directory that it always deletes.
It adds the public half to the repository, and then sets the secret to the private half with `gh secret set`, passing it on stdin.
`gh` acts with the same token as the rest of the command, given as `GH_TOKEN`.
If setting the secret fails, `ensure` deletes the key it just added.
So `ssh-keygen` and `gh` are needed to create a key, but `check` and `dump` don't use them.

GitHub can't change a key's access, so `ensure` replaces a key with the wrong access with a new pair, setting its secret as it does for a new key.
For a key without a `secret`, it adds the same public key again instead.
`ensure --rotate <title>` replaces a key and its secret with a new pair, adding the new key before it deletes the old one.
`ensure` refuses to create a key without a `secret`, as nothing would hold its private half.
It also refuses when an environment that holds a secret is missing, which `repoutils environments ensure` can create.
It never deletes a key that `pyproject.toml` doesn't declare.

`repoutils deploy-keys check` changes nothing.
It fails if a declared key is missing, has the wrong access, or shares its title with another key.
It also fails if the key's secret is missing, or was last set before the key was added, since then it can't hold the key's private half.
`ensure` can't fix a missing or stale secret without the private half, so it tells you to use `--rotate`.
`check` also fails on every key with write access that `pyproject.toml` doesn't declare, because a ruleset's deploy key bypass covers them all.
With `--fail-on-unexpected` it fails on undeclared read-only keys too.
`--exit-zero` works as it does for `labels check`.

`ensure` needs a token that can administer the repository, such as a fine-grained token with the Administration write permission and the Secrets write permission.
An environment's secret needs the Environments write permission instead of Secrets.
`check` needs read access to the same things.

`repoutils deploy-keys dump` prints every deploy key the repository has as these tables, with its access.
It can't tell which secret holds a key's private half, so add each `secret` yourself.
If two keys share a title, it prints that title once and warns.
`dump` neither reads nor changes `pyproject.toml`.

### Security settings

`[tool.repoutils.security-settings]` covers the settings on a repository's Advanced Security settings page that GitHub's REST API can read and change.
Each one is `true` or `false`.
All of them are optional, but `check` and `ensure` fail if the table doesn't set any.
A setting the table leaves out stays as it is on GitHub.

| Setting | On GitHub's Advanced Security page |
|---|---|
| `private-vulnerability-reporting` | Private vulnerability reporting |
| `dependabot-alerts` | Dependabot alerts |
| `dependabot-security-updates` | Dependabot security updates |
| `codeql-default-setup` | CodeQL analysis, with the default setup (on) or not set up (off) |
| `secret-protection` | Secret Protection |
| `push-protection` | Push protection, under Secret Protection |

The page has other settings that GitHub's API can't read or change on their own, so the table rejects them.
They are the dependency graph, automatic dependency submission, Dependabot malware alerts, grouped security updates and Copilot Autofix.
Turning on `dependabot-alerts` also turns on the dependency graph.
`codeql-default-setup` only turns CodeQL on or off, and leaves its languages and query suite to GitHub.

Some settings need others.
Dependabot security updates need Dependabot alerts, and push protection needs secret protection.
The table can't turn one on while turning the one it needs off.
If it turns one on and leaves out the one it needs, which is off on GitHub, GitHub refuses or ignores the change and `ensure` fails.
`ensure` turns settings off before it turns any on, and does each in an order that respects this.

`repoutils security-settings ensure` changes every setting that differs, then reads them all back.
It fails if GitHub didn't apply one.
That can happen when the repository's plan doesn't offer the setting, such as CodeQL on a private repository without GitHub Code Security.
It can also happen when an organization's security configuration controls the setting.
GitHub turns on CodeQL default setup by starting a run, so `ensure` waits up to two minutes for it.

`repoutils security-settings check` changes nothing, and fails in exactly the cases where `ensure` would change something.
`--exit-zero` works the same way as it does for `labels check`.

All three need a token that can administer the repository, because GitHub shows these settings only to such a token.
For a fine-grained token, that's Administration write for `ensure`, and read for `check` and `dump`.
GitHub Actions' `GITHUB_TOKEN` can't be given it.
`check` fails if the token can't see them, rather than treating them as off.

`repoutils security-settings dump` prints every setting in the table above with the value the repository has for it, under a comment giving its label on GitHub.
Paste it into `pyproject.toml` and trim it down to the settings you want to keep the same.
If you paste it in unchanged, `check` passes.
`dump` neither reads nor changes `pyproject.toml`.
If GitHub refuses to give a setting, `dump` leaves it out and says so on stderr.

## Development

See [AGENTS.md](AGENTS.md).

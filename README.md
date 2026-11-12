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
| `rulesets` | Checks, creates or updates the branch rulesets `pyproject.toml` defines, or prints the ones the repository has. |
| `environments` | Checks, creates or updates the deployment environments `pyproject.toml` defines, or prints the ones the repository has. |
| `secrets` | Checks the repository has the Actions and Dependabot secrets `pyproject.toml` names, or prints the names it has. Also checks the workflows use exactly those secrets. |
| `check-attestations` | Fails unless every distribution in `dist/` has a PEP 740 publish attestation. |

The commands that post to a PR need a GitHub token, and so do `labels`, `repo-settings`, `rulesets`, `environments` and `secrets`.
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

# A branch ruleset `repoutils rulesets` checks or creates, giving only what isn't a default; see below.
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
actions = ["CODECOV_TOKEN"]
dependabot = ["DEPENDABOT_FIX_TOKEN"]
other = ["ORG_DEPLOY_KEY"]

# The deployment environments `repoutils environments` checks or creates; see below.
[tool.repoutils.environments.pypi]
allowed-tags = ["v*"]

[tool.repoutils.environments.testpypi]
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

### Rulesets

Each `[tool.repoutils.rulesets.<name>]` table is one of the repository's branch rulesets (Settings, Rules, Rulesets).
It gives only what isn't a new ruleset's default.
Anything it leaves out is off or empty, so the table is the whole ruleset.
`ensure` turns off any rule the table leaves out.

Tag rulesets and push rulesets are left alone.
So are rulesets the repository inherits from its organization, and legacy branch protection rules.

| Setting | Values | On GitHub |
|---|---|---|
| `enforcement` | `"active"` (the default), `"evaluate"` or `"disabled"` | Whether the ruleset is enforced. |
| `include`, `exclude` | branch name patterns, such as `["main", "release/*"]`, or `"~DEFAULT_BRANCH"` or `"~ALL"` | The branches it targets. `refs/heads/` is optional. `include` is required. |
| `bypass` | a list of tables (see below) | Who can bypass the ruleset. |
| `rules.<rule>` | `true`, or a table of the rule's parameters | Each rule that's on. |

Each `bypass` entry names one actor.
That's `role` (`"admin"`, `"maintain"`, `"write"` or a custom role's ID), `app` (a GitHub App's slug), `team` (a team's slug), `org-admin = true` or `deploy-keys = true`.
It can also give a `mode`: `"always"` (the default), `"pull-request"` or `"exempt"`.

Rules and their parameters have the names that GitHub's REST API and a ruleset's JSON export use, in kebab case.
So `non_fast_forward` is `rules.non-fast-forward`.
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
Here `--fail-on-unexpected` fails on branch rulesets that `pyproject.toml` doesn't define.

All three need a token that can administer the repository, because GitHub shows a ruleset's bypass list only to such a token.
For a fine-grained token, that's Administration write for `ensure`, and read for `check` and `dump`.
GitHub Actions' `GITHUB_TOKEN` can't be given it.
If a bypass list names a team, the token also needs the organization's Members read permission, to look the team up.

`repoutils rulesets dump` prints every branch ruleset the repository has as these tables.
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
A list the table leaves out isn't checked.
Names match regardless of case, as on GitHub.
Those two cover only repository secrets.
`other` lists the secrets workflows use that the repository doesn't hold, such as an organization's or an environment's.
`check` and `dump` ignore `other`, and only `check-workflows` reads it.

`repoutils secrets` only ever reads secrets' names.
GitHub's API never returns a value, so there's no `ensure`.
Set a missing secret with `gh secret set NAME`, adding `--app dependabot` for a Dependabot secret, or on the repository's settings page.

`repoutils secrets check` fails if the repository lacks a secret the table names.
`--exit-zero` and `--fail-on-unexpected` work as they do for `labels check`.
Here `--fail-on-unexpected` fails on secrets of a listed kind that the table doesn't name.
So `dependabot = []` means there must be no Dependabot secrets.

`check` and `dump` need a token that can list the repository's secrets.
For a fine-grained token, that's the Secrets and Dependabot secrets read permissions.
GitHub Actions' `GITHUB_TOKEN` can't be given them.

`repoutils secrets dump` prints the name of every secret the repository has, as `[tool.repoutils.secrets]` with both lists.
You can paste it into `pyproject.toml`.
If you paste it in unchanged, `check --fail-on-unexpected` passes.
`dump` neither reads nor changes `pyproject.toml`.

`repoutils secrets check-workflows` needs no token, so it can run with a project's other checks.
It reads every workflow in `.github/workflows` for the secrets it uses, as `secrets.NAME` or `secrets['NAME']`.
It fails if a workflow uses a secret that none of the three lists names.
It also fails if a list names a secret that no workflow uses.
So the table can't fall behind the workflows.
It skips `GITHUB_TOKEN`, which GitHub provides, and anything in a comment.

## Development

See [AGENTS.md](AGENTS.md).

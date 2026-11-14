"""Check that a repository has the deploy keys pyproject.toml declares, and
that the secrets meant to hold their private halves exist, or create them.

Each key is a ``[tool.repoutils.deploy-keys.<title>]`` table, whose key is the
deploy key's title. ``write`` (default false) gives it write access, and
``secret = {name = ..., environment = ...}`` names the Actions secret that
holds its private half: an environment's if ``environment`` is given, else a
repository secret. The public key isn't in pyproject.toml, so rotating a key
needs no commit.

``ensure`` generates an Ed25519 key pair with ``ssh-keygen`` for each missing
key, in a private temporary directory it always deletes. It adds the public
half through the API, then sets the secret by piping the private half to
``gh secret set`` on stdin, with the same token as ``GH_TOKEN``; if that fails,
it deletes the key it added. GitHub can't change a key's access, so a key with
the wrong access is replaced with a new pair, or added again with the same
public key if it has no secret. ``--rotate <title>``
replaces both halves. It never deletes a key pyproject.toml doesn't declare.

``check`` fails if a declared key is missing, has the wrong access, or its
secret is missing or was last set before the key was added. It also fails on
any undeclared key with write access, and with ``--fail-on-unexpected`` on
undeclared read-only keys too.

``dump`` prints the repository's deploy keys as these tables, without
``secret``, which GitHub can't tell. Stdlib-only, like every repoutils command:
``gh`` and ``ssh-keygen`` are needed only to create a key."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from repoutils import config, github
from repoutils.commands import environments as environments_command

TABLE = "[tool.repoutils.deploy-keys]"
# Deploy keys per page of the list (GitHub's most).
PER_PAGE = 100


@dataclass(frozen=True)
class Secret:
    name: str
    # The environment that holds it, or None for a repository Actions secret.
    environment: str | None = None

    def describe(self) -> str:
        return f"environment {self.environment} secret {self.name}" if self.environment else f"secret {self.name}"

    def url(self, repo: str) -> str:
        name = urllib.parse.quote(self.name, safe="")
        if self.environment is None:
            return f"{github.API_URL}/repos/{repo}/actions/secrets/{name}"
        environment = urllib.parse.quote(self.environment, safe="")
        return f"{github.API_URL}/repos/{repo}/environments/{environment}/secrets/{name}"


@dataclass(frozen=True)
class DeployKey:
    title: str
    write: bool = False
    secret: Secret | None = None


def _where(title: str) -> str:
    return f"{TABLE[:-1]}.{config.toml_key(title)}]"


def _secret(where: str, value: Any) -> Secret:
    if not isinstance(value, dict):
        raise config.ConfigError(f"{where} secret must be a table with name, and optionally environment")
    unknown = sorted(set(value) - {"name", "environment"})
    if unknown:
        raise config.ConfigError(f"{where} secret has unknown setting(s): {', '.join(unknown)}")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise config.ConfigError(f"{where} secret needs a name")
    environment = value.get("environment")
    if environment is not None and (not isinstance(environment, str) or not environment):
        raise config.ConfigError(f"{where} secret environment must be a non-empty string")
    return Secret(name, environment)


def load_deploy_keys(root: Path = Path()) -> list[DeployKey] | None:
    """The deploy keys ``root/pyproject.toml`` declares, or None if it has no
    TABLE (an empty one declares that there are none), raising
    config.ConfigError (a ValueError) if they're malformed."""
    if "deploy-keys" not in config.load(root):
        return None
    keys = []
    # Each secret's destination, ignoring case as GitHub does, and the title that declares it.
    destinations: dict[tuple[str | None, str], str] = {}
    for title, entry in config.section("deploy-keys", root).items():
        where = _where(title)
        if not isinstance(entry, dict):
            raise config.ConfigError(f"{where} must be a table")
        unknown = sorted(set(entry) - {"write", "secret"})
        if unknown:
            raise config.ConfigError(f"{where} has unknown setting(s): {', '.join(unknown)}")
        write = entry.get("write", False)
        if type(write) is not bool:
            raise config.ConfigError(f"{where} write must be true or false")
        secret = _secret(where, entry["secret"]) if "secret" in entry else None
        if secret is not None:
            destination = (secret.environment and secret.environment.lower(), secret.name.upper())
            if destination in destinations:
                raise config.ConfigError(
                    f"{where} secret is also {_where(destinations[destination])}'s, "
                    "so one key's private half would overwrite the other's"
                )
            destinations[destination] = title
        keys.append(DeployKey(title, write, secret))
    return keys


def repo_keys(repo: str, token: str) -> list[dict[str, Any]]:
    """Every deploy key the repository has."""
    keys: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = github.request("GET", f"{github.API_URL}/repos/{repo}/keys?per_page={PER_PAGE}&page={page}", token)
        keys += batch
        if len(batch) < PER_PAGE:
            return keys
        page += 1


def _secret_updated(repo: str, token: str, secret: Secret) -> datetime | None:
    """When secret was last set, or None if it's missing."""
    try:
        found = github.request("GET", secret.url(repo), token)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        e.close()
        return None
    return datetime.fromisoformat(found["updated_at"])


def _access(write: bool) -> str:
    return "write" if write else "read-only"


@dataclass
class Status:
    """How a declared key stands on GitHub."""

    key: DeployKey
    # The keys with its title on GitHub.
    existing: list[dict[str, Any]]
    # Whether its secret's environment is missing (None if there's no secret).
    environment_missing: bool = False
    # When its secret was last set, or None if it's missing (or there's none).
    secret_updated: datetime | None = None

    @property
    def found(self) -> dict[str, Any] | None:
        return self.existing[0] if len(self.existing) == 1 else None

    @property
    def wrong_access(self) -> bool:
        return self.found is not None and bool(self.found["read_only"]) == self.key.write

    def secret_problem(self) -> str | None:
        """Why the secret can't hold the key's private half, or None if it may."""
        secret = self.key.secret
        if secret is None:
            return None
        if self.environment_missing:
            return f"environment {secret.environment}, which holds its secret {secret.name}, is missing"
        if self.secret_updated is None:
            return f"{secret.describe()} is missing"
        found = self.found
        if found is not None and self.secret_updated < datetime.fromisoformat(found["created_at"]):
            return f"{secret.describe()} was last set before the key was added, so it can't hold its private half"
        return None

    def _duplicates(self) -> str:
        """What to do about several keys with the title, as a rotation that failed
        after setting the secret leaves: which to keep, if the secret tells."""
        by_age = sorted(self.existing, key=lambda found: (datetime.fromisoformat(found["created_at"]), found["id"]))
        newest, older = by_age[-1], by_age[:-1]
        ids = ", ".join(str(found["id"]) for found in older)
        text = f"{len(self.existing)} deploy keys have this title"
        if self.secret_updated is not None and self.secret_updated >= datetime.fromisoformat(newest["created_at"]):
            # The times can't show which private half the secret holds, only that it can be the newest's.
            return (
                f"{text}: its secret was set after the newest (id {newest['id']}), so if it holds that key's "
                f"private half, delete the others on GitHub (id {ids}); if unsure, delete them all and run ensure"
            )
        every = ", ".join(str(found["id"]) for found in by_age)
        return f"{text} (ids {every}, oldest first): delete all but one on GitHub"

    def problems(self) -> list[str]:
        if not self.existing:
            # ensure can't create the key until its secret's environment exists, so say so now.
            secret = self.secret_problem() if self.environment_missing else None
            return ["missing"] + ([secret] if secret else [])
        if len(self.existing) > 1:
            return [self._duplicates()]
        problems = []
        if self.wrong_access:
            problems.append(f"{_access(not self.key.write)} on GitHub, not {_access(self.key.write)}")
        secret = self.secret_problem()
        if secret:
            problems.append(secret)
        return problems


def statuses(repo: str, token: str, keys: Sequence[DeployKey]) -> tuple[list[Status], list[dict[str, Any]]]:
    """How each declared key stands, and every deploy key the repository has."""
    existing = repo_keys(repo, token)
    environments: set[str] | None = None
    results = []
    for key in keys:
        status = Status(key, [found for found in existing if found["title"] == key.title])
        if key.secret is not None:
            if key.secret.environment is not None:
                if environments is None:
                    environments = {env["name"].lower() for env in environments_command.repo_environments(repo, token)}
                status.environment_missing = key.secret.environment.lower() not in environments
            if not status.environment_missing:
                status.secret_updated = _secret_updated(repo, token, key.secret)
        results.append(status)
    return results, existing


def undeclared(keys: Iterable[DeployKey], existing: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The keys in existing whose title keys doesn't declare."""
    titles = {key.title for key in keys}
    return [found for found in existing if found["title"] not in titles]


def check(repo: str, token: str, keys: list[DeployKey], *, exit_zero: bool, fail_on_unexpected: bool) -> int:
    results, existing = statuses(repo, token, keys)
    failing = 0
    for status in results:
        problems = status.problems()
        for problem in problems:
            print(f"{status.key.title}: {problem}")
        failing += bool(problems)
    unexpected = [found for found in undeclared(keys, existing) if fail_on_unexpected or not found["read_only"]]
    for found in unexpected:
        print(f"{found['title']}: {_access(not found['read_only'])} deploy key not declared in pyproject.toml")
    if failing:
        print(f"{failing} of {len(keys)} deploy key(s) in {repo} need `repoutils deploy-keys ensure`.")
    if unexpected:
        print(
            f"{len(unexpected)} deploy key(s) in {repo} aren't declared in pyproject.toml: "
            "declare them there (`repoutils deploy-keys dump` prints them) or delete them on GitHub."
        )
    if failing or unexpected:
        return 0 if exit_zero else 1
    print(f"All {len(keys)} deploy key(s) in {repo} match pyproject.toml.")
    return 0


def generate(title: str, directory: Path) -> tuple[str, bytes]:
    """A new Ed25519 key pair, generated in directory: its public half, and its private half."""
    path = directory / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", title, "-f", str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    return path.with_suffix(".pub").read_text().strip(), path.read_bytes()


def set_secret(repo: str, token: str, secret: Secret, value: bytes) -> None:
    """Set secret to value with ``gh secret set``, acting with token."""
    # Name the host, so a GH_HOST for another one doesn't send the secret there.
    host = urllib.parse.urlsplit(github.API_URL).netloc.removeprefix("api.")
    command = ["gh", "secret", "set", secret.name, "--repo", f"{host}/{repo}"]
    if secret.environment is not None:
        command += ["--env", secret.environment]
    environ = {**os.environ, "GH_TOKEN": token}
    result = subprocess.run(command, input=value, capture_output=True, env=environ, check=False)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command, stderr=result.stderr.decode(errors="replace"))


def _add(repo: str, token: str, title: str, public: str, write: bool) -> dict[str, Any]:
    body = {"title": title, "key": public, "read_only": not write}
    added: dict[str, Any] = github.request("POST", f"{github.API_URL}/repos/{repo}/keys", token, json_body=body)
    return added


def _delete(repo: str, token: str, found: dict[str, Any]) -> None:
    github.request("DELETE", f"{github.API_URL}/repos/{repo}/keys/{found['id']}", token)


def replace(repo: str, token: str, key: DeployKey, old: Iterable[dict[str, Any]]) -> None:
    """Add a new key pair for key, set its secret to the private half, and only
    then delete old; if setting the secret fails, delete the new key instead."""
    assert key.secret is not None
    with tempfile.TemporaryDirectory(prefix="repoutils-deploy-key-") as directory:
        public, private = generate(key.title, Path(directory))
        added = _add(repo, token, key.title, public, key.write)
        try:
            set_secret(repo, token, key.secret, private)
        except BaseException as error:
            # Report a failed clean-up, but raise the error that caused it.
            cleaned = True
            try:
                _delete(repo, token, added)
            except Exception as cleanup:
                if isinstance(cleanup, urllib.error.HTTPError):
                    cleanup.close()
                cleaned = False
                print(
                    f"{key.title}: couldn't delete the key just added (id {added['id']}): {cleanup}: "
                    "delete it on GitHub",
                    file=sys.stderr,
                )
            if not isinstance(error, subprocess.CalledProcessError):
                # gh may have set the secret before the interruption, to the new key's private half.
                then = "" if cleaned else f"once key id {added['id']} is deleted, "
                print(
                    f"{key.title}: interrupted while setting {key.secret.describe()}, which may now hold no key's "
                    f"private half: {then}run `repoutils deploy-keys ensure --rotate {shlex.quote(key.title)}`",
                    file=sys.stderr,
                )
            raise
    for found in old:
        _delete(repo, token, found)


def _replacing(status: Status, rotate: set[str]) -> bool:
    """Whether ensure generates a new key pair for status's key: if it's
    missing, rotated, or has a secret and the wrong access (adding the same
    public key again would make the secret look older than the key)."""
    return not status.existing or status.key.title in rotate or (status.wrong_access and status.key.secret is not None)


def _preflight(results: Sequence[Status], rotate: set[str]) -> list[str]:
    """Why ensure can't do what results need, if it can't, before it changes anything."""
    errors = []
    for status in results:
        key = status.key
        if len(status.existing) > 1:
            errors.append(f"{key.title}: {status.problems()[0]}")
        elif _replacing(status, rotate) and key.secret is None:
            errors.append(
                f"{key.title}: no secret is declared to hold the private half of a new key: "
                "give it a secret, or add the key on GitHub"
            )
        elif status.environment_missing:
            errors.append(f"{key.title}: {status.secret_problem()}: create it with `repoutils environments ensure`")
        elif not _replacing(status, rotate) and status.secret_problem():
            errors.append(
                f"{key.title}: {status.secret_problem()}: replace the key with `--rotate {shlex.quote(key.title)}`"
            )
    return errors


def ensure(repo: str, token: str, keys: list[DeployKey], rotate: Sequence[str] = ()) -> int:
    unknown = sorted(set(rotate) - {key.title for key in keys})
    if unknown:
        print(f"--rotate names deploy key(s) pyproject.toml doesn't declare: {', '.join(unknown)}", file=sys.stderr)
        return 1
    results, existing = statuses(repo, token, keys)
    errors = _preflight(results, set(rotate))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    generating = {status.key.title for status in results if _replacing(status, set(rotate))}
    missing = [tool for tool in ("ssh-keygen", "gh") if generating and shutil.which(tool) is None]
    if missing:
        print(f"Creating a deploy key needs {' and '.join(missing)} on PATH.", file=sys.stderr)
        return 1
    changed = 0
    for status in results:
        key, found = status.key, status.found
        if key.title in generating:
            assert key.secret is not None  # _preflight refuses a new key without one
            replace(repo, token, key, status.existing)
            done = "replaced with a new key pair" if status.existing else "created"
            print(f"{key.title}: {done}, and {key.secret.describe()} set")
        elif status.wrong_access and found is not None:
            _delete(repo, token, found)
            try:
                _add(repo, token, key.title, found["key"], key.write)
            except urllib.error.HTTPError:
                print(f"{key.title}: deleted, but adding it again failed; its public key is {found['key']}")
                raise
            print(f"{key.title}: given {_access(key.write)} access")
        else:
            continue
        changed += 1
    for found in undeclared(keys, existing):
        if not found["read_only"]:
            print(f"{found['title']}: write deploy key not declared in pyproject.toml (ensure leaves it alone)")
    # Check what GitHub now has, as `check` would.
    after, _ = statuses(repo, token, keys)
    failed = [(status.key.title, problem) for status in after for problem in status.problems()]
    for title, problem in failed:
        print(f"After ensure, {title}: {problem}", file=sys.stderr)
    if failed:
        return 1
    print(f"Created or changed {changed} of {len(keys)} deploy key(s) in {repo}.")
    return 0


def dump_toml(existing: Iterable[dict[str, Any]]) -> str:
    """GitHub's deploy keys as TABLE's sub-tables, each title once."""
    entries: dict[str, bool] = {}
    for found in existing:
        entries[found["title"]] = entries.get(found["title"], False) or not found["read_only"]
    return "\n".join(f"{_where(title)}\nwrite = {config.toml_value(write)}\n" for title, write in entries.items())


def dump(repo: str, token: str) -> int:
    existing = repo_keys(repo, token)
    if not existing:
        print(f"{repo} has no deploy keys.", file=sys.stderr)
    titles = [found["title"] for found in existing]
    for title in dict.fromkeys(title for title in titles if titles.count(title) > 1):
        print(f"{titles.count(title)} deploy keys are titled {title!r}, so it's printed once.", file=sys.stderr)
    print(dump_toml(existing), end="")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="repoutils deploy-keys",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        epilog="Uses $GITHUB_TOKEN, defaulting to `gh auth token`. It needs the Administration permission "
        "(write for ensure). A repository secret needs Secrets (write for ensure); an environment's secret "
        "needs Environments (write for ensure) and Actions read.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, metavar="{check,ensure,dump}")
    check_parser = subparsers.add_parser(
        "check",
        help="fail if a declared key or its secret is missing, or there's an undeclared write key",
        description="Fail if a declared deploy key is missing or has the wrong access, if its secret is missing "
        "or older than the key, or if the repository has a write key pyproject.toml doesn't declare.",
    )
    ensure_parser = subparsers.add_parser(
        "ensure",
        help="create missing keys, setting their secrets, and fix their access",
        description="Create each missing deploy key, setting its secret to the private half, "
        "and fix each key's access. Needs ssh-keygen and gh to create a key.",
    )
    dump_parser = subparsers.add_parser(
        "dump",
        help="print the repository's deploy keys as pyproject.toml tables",
        description=f"Print every deploy key the repository has as {TABLE} sub-tables, to paste into pyproject.toml.",
    )
    for sub in (check_parser, ensure_parser, dump_parser):
        sub.add_argument(
            "--repo",
            default=os.environ.get("GITHUB_REPOSITORY"),
            help="owner/name (default: $GITHUB_REPOSITORY, else gh's current repo)",
        )
    check_parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="exit 0 even if keys need ensure (still exit 1 on an error, unlike `|| true`)",
    )
    check_parser.add_argument(
        "--fail-on-unexpected",
        action="store_true",
        help="also fail on undeclared read-only keys (undeclared write keys always fail)",
    )
    ensure_parser.add_argument(
        "--rotate",
        action="append",
        default=[],
        metavar="TITLE",
        help="replace this key and its secret with a new key pair (may be repeated)",
    )
    args = parser.parse_args(argv)

    try:
        # dump doesn't read pyproject.toml, so it works before there are any keys there.
        keys = [] if args.mode == "dump" else load_deploy_keys()
        if keys is None:
            print(f"No deploy keys are declared: add {TABLE[:-1]}.<title>] to pyproject.toml.", file=sys.stderr)
            return 1
        token = os.environ.get("GITHUB_TOKEN") or github.gh_output("auth", "token")
        repo = args.repo or github.gh_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if args.mode == "dump":
            return dump(repo, token)
        if args.mode == "check":
            return check(repo, token, keys, exit_zero=args.exit_zero, fail_on_unexpected=args.fail_on_unexpected)
        return ensure(repo, token, keys, args.rotate)
    except subprocess.CalledProcessError as e:
        print(f"{' '.join(e.cmd)} failed:\n{e.stderr}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} from {e.filename}:\n{detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        return 1


run = main

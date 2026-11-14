"""Wait for a PR's base commit to have its project coverage commit status.

A PR's coverage is compared with its base commit's ``coverage/project`` status
(named by ``[tool.repoutils.coverage] project-status``), which the base's own
CI posts with ``post-coverage-status``: ``post-patch-coverage --base`` shows
the change in the PR comment, and ``post-coverage-status --base`` in the PR's
own status. So when a PR's CI runs alongside its base's (e.g. a stacked PR
rebased onto the default branch just as its parent merges), the PR's coverage
can be ready before the base's status is there to compare with.

Run this on PRs before either of those. It polls GitHub for the base commit's
status and returns as soon as it's there, which is the first request unless
the base's CI is still running. Until then it checks the base's latest
``--check-name`` check run (the job that posts the status). Once that has
finished, the status is either there or never coming (the job failed, was
cancelled or was skipped, e.g. because the base's tests failed), so this gives
up straight away. It never falls back to an older commit, whose figure would
mix in the changes of whatever merged in between.

It never fails the job: giving up (then, or after ``--timeout``) prints a
GitHub Actions warning and exits 0, as the base's CI failing (e.g. the default
branch is broken and this PR fixes it) mustn't fail the PR's own required
check. The comparison then says the base has no figure.

Its requests send ``$GITHUB_TOKEN`` (it needs ``checks: read`` and
``statuses: read``), so it never follows a redirect with it.
"""

import argparse
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from repoutils import coverage_status
from repoutils.github import no_redirect_opener

GITHUB_API = "https://api.github.com"
DEFAULT_CHECK_NAME = "post-coverage-status"


def _get_json(url: str, token: str | None, timeout: float) -> tuple[int, object]:
    """GET ``url`` from GitHub's API, returning its status and JSON body
    (``None`` if not JSON). A redirect is reported as its status rather than
    followed, as urlopen would follow it with the token, even to another host."""
    headers = {"Accept": "application/json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with no_redirect_opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def base_status(repo: str, sha: str, name: str, token: str | None, timeout: float) -> tuple[bool, str]:
    """Whether ``sha`` has a status called ``name``, and its description or why not."""
    page = 1
    while True:
        url = f"{GITHUB_API}/repos/{repo}/commits/{sha}/status?per_page=100&page={page}"
        try:
            status, body = _get_json(url, token, timeout)
        except (OSError, http.client.HTTPException) as e:
            return False, f"request failed: {getattr(e, 'reason', e)}"
        statuses = body.get("statuses") if status == 200 and isinstance(body, dict) else None
        if not isinstance(statuses, list):
            return False, f"HTTP {status}"
        # The combined status holds only the latest status for each name, which GitHub matches ignoring case.
        for item in statuses:
            if isinstance(item, dict) and str(item.get("context", "")).casefold() == name.casefold():
                return True, str(item.get("description") or "")
        if len(statuses) < 100:
            return False, f"no {name} status yet"
        page += 1


def base_ci_state(repo: str, sha: str, check_name: str, token: str | None, timeout: float) -> tuple[str, str]:
    """The state of ``sha``'s latest ``check_name`` check run, and a description.

    The state is ``running`` (also when it can't be fetched, or there's no such
    check run yet, as a job is given one only once the jobs it needs have
    finished) or ``finished``.
    """
    query = urllib.parse.urlencode({"check_name": check_name, "filter": "latest"})
    url = f"{GITHUB_API}/repos/{repo}/commits/{sha}/check-runs?{query}"
    try:
        status, body = _get_json(url, token, timeout)
    except (OSError, http.client.HTTPException) as e:
        return "running", f"request failed: {getattr(e, 'reason', e)}"
    runs = body.get("check_runs") if status == 200 and isinstance(body, dict) else None
    if not isinstance(runs, list):
        return "running", f"HTTP {status}"
    if not runs:
        return "running", f"no {check_name} check run yet"
    # filter=latest keeps one run per name; take the newest in case of more.
    run = max(runs, key=lambda r: r.get("id", 0))
    if run.get("status") != "completed":
        return "running", f"{check_name} {run.get('status')}"
    return "finished", f"{check_name} {run.get('conclusion')}"


def _positive(value: str) -> float:
    seconds = float(value)
    if not seconds > 0:
        raise argparse.ArgumentTypeError(f"must be positive, not {value}")
    return seconds


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="repoutils wait-for-base-coverage", description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="the repository, as owner/name")
    parser.add_argument("sha", help="the PR's base commit")
    parser.add_argument(
        "--check-name",
        default=DEFAULT_CHECK_NAME,
        help="the CI job that posts the base's coverage status (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600,
        help="seconds to wait in all (default: %(default)s)",
    )
    parser.add_argument(
        "--interval",
        type=_positive,
        default=10,
        help="seconds between attempts (default: %(default)s)",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30,
        help="seconds to wait for one response (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = _parse_args(argv)
    # ConfigError reaches cli.main(), which prints it and exits 1: a bad setting isn't the base's CI failing.
    name, _ = coverage_status.status_names()
    token = os.environ.get("GITHUB_TOKEN") or None

    deadline = clock() + args.timeout

    def request_timeout() -> float:
        # Don't let slow requests run far past the deadline.
        return float(min(args.request_timeout, max(deadline - clock(), 1)))

    attempt = 0
    while True:
        attempt += 1
        found, status = base_status(args.repo, args.sha, name, token, request_timeout())
        if found:
            print(f"Attempt {attempt}: base commit {args.sha} has its {name} status ({status})")
            return 0
        state, ci = base_ci_state(args.repo, args.sha, args.check_name, token, request_timeout())
        print(f"Attempt {attempt}: {status}; base CI: {ci}", flush=True)
        if state == "finished":
            # The job may have posted the status and finished since it was read.
            found, status = base_status(args.repo, args.sha, name, token, request_timeout())
            if found:
                print(f"Base commit {args.sha} has its {name} status ({status})")
                return 0
            print(f"::warning::Base commit {args.sha} has no {name} status, and its CI has finished ({ci})")
            return 0
        if clock() + args.interval > deadline:
            print(
                f"::warning::Base commit {args.sha} still has no {name} status after {args.timeout:g} seconds "
                f"({status}; base CI: {ci})"
            )
            return 0
        sleep(args.interval)


def run(argv: list[str]) -> int:
    return main(argv)

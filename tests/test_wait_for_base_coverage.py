import contextlib
import http.client
import http.server
import io
import json
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any, override
from unittest import TestCase, mock

from repoutils.commands import wait_for_base_coverage
from repoutils.commands.wait_for_base_coverage import base_ci_state, base_status
from repoutils.commands.wait_for_base_coverage import main as wait_for_base_coverage_main
from tests.test_github import NO_REDIRECT_OPEN

DESCRIPTION = "96.52% (2114/2172 lines, 610/650 branches)"


class WaitForBaseCoverageTests(TestCase):
    REPO = "owner/repo"
    SHA = "abc123"

    def _response(self, status: int, payload: bytes) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value.status = status
        response.__enter__.return_value.read.return_value = payload
        return response

    def _http_error(self, status: int, payload: bytes) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://example/", status, "Error", Message(), io.BytesIO(payload))

    def _call(self, func: Callable[..., Any], result: object, *args: Any) -> tuple[Any, urllib.request.Request]:
        kwargs: dict[str, Any] = {"side_effect" if isinstance(result, Exception) else "return_value": result}
        with mock.patch(NO_REDIRECT_OPEN, **kwargs) as urlopen:
            outcome = func(self.REPO, self.SHA, *args, 7)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 7})
        return outcome, urlopen.call_args.args[0]

    def _statuses(self, *contexts: str) -> mock.MagicMock:
        statuses = [{"context": c, "description": f"{c} says {DESCRIPTION}"} for c in contexts]
        return self._response(200, json.dumps({"state": "success", "statuses": statuses}).encode())

    def test_base_status_found(self) -> None:
        outcome, request = self._call(
            base_status, self._statuses("lint", "Coverage/Project"), "coverage/project", "t0k"
        )
        self.assertEqual(outcome, (True, f"Coverage/Project says {DESCRIPTION}"))
        self.assertEqual(
            request.full_url, "https://api.github.com/repos/owner/repo/commits/abc123/status?per_page=100&page=1"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer t0k")

    def test_base_status_missing(self) -> None:
        outcome, request = self._call(base_status, self._statuses("lint"), "coverage/project", None)
        self.assertEqual(outcome, (False, "no coverage/project status yet"))
        self.assertFalse(request.has_header("Authorization"))

    def test_base_status_pages(self) -> None:
        pages = [self._statuses(*(f"s{i}" for i in range(100))), self._statuses("coverage/project")]
        with mock.patch(NO_REDIRECT_OPEN, side_effect=pages) as urlopen:
            found, _ = base_status(self.REPO, self.SHA, "coverage/project", None, 7)
        self.assertTrue(found)
        self.assertTrue(urlopen.call_args.args[0].full_url.endswith("&page=2"))

    def test_base_status_errors(self) -> None:
        self.assertEqual(
            self._call(base_status, self._http_error(404, b'{"message": "No commit"}'), "coverage/project", None)[0],
            (False, "HTTP 404"),
        )
        self.assertEqual(
            self._call(base_status, self._response(200, b"<html>"), "coverage/project", None)[0],
            (False, "HTTP 200"),
        )
        self.assertEqual(
            self._call(base_status, urllib.error.URLError("timed out"), "coverage/project", None)[0],
            (False, "request failed: timed out"),
        )

    def _check_runs(
        self, *runs: dict[str, Any], token: str | None = None
    ) -> tuple[tuple[str, str], urllib.request.Request]:
        body = json.dumps({"total_count": len(runs), "check_runs": list(runs)}).encode()
        with mock.patch(NO_REDIRECT_OPEN, return_value=self._response(200, body)) as urlopen:
            outcome = base_ci_state(self.REPO, self.SHA, "post", token, 7)
        return outcome, urlopen.call_args.args[0]

    def test_base_ci_running(self) -> None:
        outcome, request = self._check_runs({"id": 1, "status": "in_progress", "conclusion": None})
        self.assertEqual(outcome, ("running", "post in_progress"))
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/owner/repo/commits/abc123/check-runs?check_name=post&filter=latest",
        )
        self.assertFalse(request.has_header("Authorization"))
        # A job gets its check run only once the jobs it needs have finished.
        self.assertEqual(self._check_runs()[0], ("running", "no post check run yet"))

    def test_base_ci_finished(self) -> None:
        outcome, request = self._check_runs(
            {"id": 2, "status": "completed", "conclusion": "skipped"},
            {"id": 1, "status": "in_progress", "conclusion": None},
            token="t0k",
        )
        self.assertEqual(outcome, ("finished", "post skipped"))
        self.assertEqual(request.get_header("Authorization"), "Bearer t0k")
        self.assertEqual(
            self._check_runs({"id": 1, "status": "completed", "conclusion": "success"})[0],
            ("finished", "post success"),
        )

    def test_base_ci_errors(self) -> None:
        self.assertEqual(
            self._call(base_ci_state, self._http_error(403, b'{"message": "no"}'), "post", None)[0],
            ("running", "HTTP 403"),
        )
        self.assertEqual(
            self._call(base_ci_state, http.client.IncompleteRead(b""), "post", None)[0],
            ("running", "request failed: IncompleteRead(0 bytes read)"),
        )

    def test_redirect_not_followed(self) -> None:
        # A real server, as it's urllib's own redirect handling (which would
        # send the token on to another host) that mustn't run.
        seen: list[tuple[str, str | None]] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                assert isinstance(self.server, http.server.HTTPServer)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            @override
            def log_message(self, format: str, *args: Any) -> None:
                pass

        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with mock.patch.object(wait_for_base_coverage, "GITHUB_API", f"http://127.0.0.1:{server.server_port}"):
                    ci = base_ci_state(self.REPO, self.SHA, "post", "t0k", 7)
                    status = base_status(self.REPO, self.SHA, "coverage/project", "t0k", 7)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(ci, ("running", "HTTP 302"))
        self.assertEqual(status, (False, "HTTP 302"))
        self.assertEqual(
            seen,
            [
                ("/repos/owner/repo/commits/abc123/check-runs?check_name=post&filter=latest", "Bearer t0k"),
                ("/repos/owner/repo/commits/abc123/status?per_page=100&page=1", "Bearer t0k"),
            ],
        )

    def _main(
        self, statuses: list[tuple[bool, str]], ci: list[tuple[str, str]], *args: str, pyproject: str = ""
    ) -> tuple[int, list[float], str, mock.MagicMock, mock.MagicMock]:
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        out = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            contextlib.chdir(tmp),
            mock.patch("repoutils.commands.wait_for_base_coverage.base_status", side_effect=statuses) as status_mock,
            mock.patch("repoutils.commands.wait_for_base_coverage.base_ci_state", side_effect=ci) as ci_mock,
            mock.patch.dict("os.environ", {"GITHUB_TOKEN": "t0k"}),
            contextlib.redirect_stdout(out),
        ):
            Path("pyproject.toml").write_text(pyproject)
            code = wait_for_base_coverage_main([self.REPO, self.SHA, *args], clock=lambda: now[0], sleep=sleep)
        return code, sleeps, out.getvalue(), status_mock, ci_mock

    def test_base_already_has_status(self) -> None:
        code, sleeps, out, statuses, ci = self._main([(True, DESCRIPTION)], [])
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [])
        self.assertEqual(out, f"Attempt 1: base commit abc123 has its coverage/project status ({DESCRIPTION})\n")
        self.assertEqual(statuses.call_args.args, (self.REPO, self.SHA, "coverage/project", "t0k", 30))
        ci.assert_not_called()

    def test_configured_status_name(self) -> None:
        _, _, out, statuses, _ = self._main(
            [(True, DESCRIPTION)], [], pyproject='[tool.repoutils.coverage]\nproject-status = "cov"\n'
        )
        self.assertEqual(statuses.call_args.args[2], "cov")
        self.assertIn("its cov status", out)

    def test_waits_for_base_ci(self) -> None:
        code, sleeps, out, _, ci = self._main(
            [(False, "no coverage/project status yet")] * 2 + [(True, DESCRIPTION)],
            [("running", "no post-coverage-status check run yet"), ("running", "post-coverage-status queued")],
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10])
        self.assertIn(
            "Attempt 1: no coverage/project status yet; base CI: no post-coverage-status check run yet\n", out
        )
        self.assertNotIn("::warning::", out)
        self.assertEqual(ci.call_args.args, (self.REPO, self.SHA, "post-coverage-status", "t0k", 30))

    def test_gives_up_at_once_if_base_ci_finished_without_it(self) -> None:
        code, sleeps, out, statuses, _ = self._main(
            [(False, "no coverage/project status yet")] * 3,
            [("running", "post-coverage-status in_progress"), ("finished", "post-coverage-status skipped")],
            "--check-name",
            "post-coverage-status",
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10])
        # Read once more after the job finished, in case it posted in between.
        self.assertEqual(statuses.call_count, 3)
        self.assertTrue(
            out.endswith(
                "::warning::Base commit abc123 has no coverage/project status, "
                "and its CI has finished (post-coverage-status skipped)\n"
            )
        )

    def test_status_posted_just_before_the_job_finished(self) -> None:
        code, sleeps, out, _, _ = self._main(
            [(False, "no coverage/project status yet"), (True, DESCRIPTION)],
            [("finished", "post-coverage-status success")],
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [])
        self.assertTrue(out.endswith(f"Base commit abc123 has its coverage/project status ({DESCRIPTION})\n"))

    def test_gives_up_after_timeout(self) -> None:
        code, sleeps, out, _, _ = self._main(
            [(False, "no coverage/project status yet")] * 10,
            [("running", "post-coverage-status in_progress")] * 10,
            "--timeout",
            "25",
        )
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 10])
        self.assertIn("::warning::Base commit abc123 still has no coverage/project status after 25 seconds", out)

    def test_request_timeout_capped_by_deadline(self) -> None:
        now = [0.0]
        timeouts = []

        def request(*args: Any) -> tuple[Any, str]:
            timeouts.append(args[-1])
            now[0] += 4
            return (False if len(timeouts) % 2 else "running"), "slow"

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with (
            tempfile.TemporaryDirectory() as tmp,
            contextlib.chdir(tmp),
            mock.patch("repoutils.commands.wait_for_base_coverage.base_status", side_effect=request),
            mock.patch("repoutils.commands.wait_for_base_coverage.base_ci_state", side_effect=request),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = wait_for_base_coverage_main(
                [self.REPO, self.SHA, "--timeout", "20", "--interval", "1", "--request-timeout", "15"],
                clock=lambda: now[0],
                sleep=sleep,
            )
        self.assertEqual(code, 0)
        self.assertEqual(timeouts, [15, 15, 11, 7, 2, 1])

    def test_rejects_a_non_positive_interval(self) -> None:
        for interval in ("0", "-1"):
            with self.subTest(interval=interval), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit):
                    wait_for_base_coverage_main([self.REPO, self.SHA, "--interval", interval])
                self.assertIn("must be positive", err.getvalue())

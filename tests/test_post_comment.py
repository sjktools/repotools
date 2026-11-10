import contextlib
import io
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any, NoReturn
from unittest import TestCase, mock

from repoutils import github as github_comments
from repoutils.commands.post_comment import main as post_comment_main

EXOLOT_MARKER = "<!-- repoutils-exolot-findings -->"
HIDE = ["--name", "exolot-findings", "--hide-previous"]


class FakeGitHub:
    """Answers github_comments.request like the API would, for PR 7 of o/r with
    head "abc123" and the given comments (GraphQL nodes)."""

    def __init__(self, comments: list[dict[str, Any]], pages: int = 1) -> None:
        self.comments = comments
        self.pages = pages
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        self.calls.append((method, url, json_body))
        if url.endswith("/pulls/7"):
            return {"head": {"sha": "abc123"}}
        if url.endswith("/issues/7/comments"):
            return {"node_id": "NEW"}
        assert url.endswith("/graphql")
        if "minimizeComment" in json_body["query"]:
            return {"data": {"minimizeComment": {"clientMutationId": None}}}
        page = 0 if json_body["variables"]["cursor"] is None else int(json_body["variables"]["cursor"])
        per_page = -(-len(self.comments) // self.pages) if self.comments else 1
        nodes = self.comments[page * per_page : (page + 1) * per_page]
        more = page + 1 < self.pages
        comments = {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": str(page + 1) if more else None}}
        return {"data": {"repository": {"pullRequest": {"comments": comments}}}}

    def hidden(self) -> list[str]:
        return [b["variables"]["id"] for m, u, b in self.calls if b and "minimizeComment" in b.get("query", "")]


def _exolot_comment(
    node_id: str, *, mine: bool = True, minimized: bool = False, body: str = EXOLOT_MARKER + "\nold"
) -> dict[str, Any]:
    return {"id": node_id, "viewerDidAuthor": mine, "isMinimized": minimized, "body": body}


class PostCommentHidePreviousTests(TestCase):
    def _report(self) -> Path:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        path = directory / "report.md"
        path.write_text("new\n")
        return path

    def _run(self, argv: list[str], request: Any) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}),
            mock.patch.object(github_comments, "request", request),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = post_comment_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_hides_only_own_earlier_reports(self) -> None:
        for pages in (1, 2):
            with self.subTest(pages=pages):
                gh = FakeGitHub(
                    [
                        _exolot_comment("OLD1"),
                        _exolot_comment("THEIRS", mine=False),
                        _exolot_comment("HIDDEN", minimized=True),
                        _exolot_comment("OTHER", body="<!-- something else -->"),
                        _exolot_comment("OLD2"),
                        _exolot_comment("NEW"),
                    ],
                    pages=pages,
                )
                code, out, _ = self._run(["--repo", "o/r", "7", str(self._report()), "--commit", "abc", *HIDE], gh)
                self.assertEqual(code, 0)
                posts = [(u, b) for m, u, b in gh.calls if m == "POST" and u.endswith("/issues/7/comments")]
                self.assertEqual(
                    posts,
                    [("https://api.github.com/repos/o/r/issues/7/comments", {"body": EXOLOT_MARKER + "\nnew\n"})],
                )
                self.assertEqual(gh.hidden(), ["OLD1", "OLD2"])
                self.assertIn("hid 2 earlier comment(s)", out)

    def test_skips_a_stale_commit(self) -> None:
        gh = FakeGitHub([])
        code, out, _ = self._run(["--repo", "o/r", "7", str(self._report()), "--commit", "def", *HIDE], gh)
        self.assertEqual(code, 0)
        self.assertEqual([m for m, u, b in gh.calls], ["GET"])
        self.assertIn("Not posting", out)

    def test_reports_graphql_errors(self) -> None:
        def request(method: str, url: str, token: str, **kwargs: object) -> dict[str, Any]:
            if url.endswith("/graphql"):
                return {"errors": [{"message": "nope"}]}
            return {"node_id": "NEW"}

        code, _, err = self._run(["--repo", "o/r", "7", str(self._report()), *HIDE], request)
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_reports_http_errors(self) -> None:
        def request(method: str, url: str, token: str, **kwargs: object) -> NoReturn:
            raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), io.BytesIO(b"no access"))

        code, _, err = self._run(["--repo", "o/r", "7", str(self._report()), *HIDE], request)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", err)

    def test_reports_gh_failure(self) -> None:
        def run(cmd: list[str], **kwargs: object) -> NoReturn:
            raise subprocess.CalledProcessError(4, cmd, stderr="not logged in")

        err = io.StringIO()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(subprocess, "run", run),
            contextlib.redirect_stderr(err),
        ):
            code = post_comment_main(["7", str(self._report()), "--repo", "o/r", *HIDE])
        self.assertEqual(code, 1)
        self.assertIn("gh auth token failed:\nnot logged in", err.getvalue())


class PostCommentEditTests(TestCase):
    """Without --hide-previous, the comment is edited in place."""

    def test_edits_the_earlier_comment_with_the_configured_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text('[tool.repoutils]\ncomment-prefix = "proj"\n')
            report = root / "r.md"
            report.write_text("new\n")
            calls: list[tuple[str, str, Any]] = []

            def request(method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
                calls.append((method, url, json_body))
                if method == "GET" and url.endswith("/comments?per_page=100&page=1"):
                    return [{"id": 5, "user": {"login": "bot"}, "body": "<!-- proj-notes -->\nold"}]
                return []

            out = io.StringIO()
            with (
                contextlib.chdir(root),
                mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok"}),
                mock.patch.object(github_comments, "request", request),
                contextlib.redirect_stdout(out),
            ):
                code = post_comment_main(["7", str(report), "--repo", "o/r", "--name", "notes", "--author", "bot"])
        self.assertEqual(code, 0)
        writes = [(m, u, b) for m, u, b in calls if m != "GET"]
        self.assertEqual(
            writes,
            [("PATCH", "https://api.github.com/repos/o/r/issues/comments/5", {"body": "<!-- proj-notes -->\nnew\n"})],
        )
        self.assertIn("Posted", out.getvalue())

    def test_name_is_required(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            post_comment_main(["7", "r.md"])
        self.assertIn("--name", err.getvalue())

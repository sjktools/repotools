import contextlib
import io
import re
import subprocess
import tempfile
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any, override
from unittest import TestCase, mock

from repoutils import github as github_comments
from repoutils.commands import added_strings
from repoutils.commands.added_strings import (
    MARKER,
    Config,
    Finding,
    Location,
    Rule,
    added_lines,
    code_span,
    find,
    load_config,
    render,
)
from repoutils.config import ConfigError

# The tests look for made-up strings, so this file doesn't add the ones
# repoutils' own pyproject.toml configures.
_PYPROJECT = """
[tool.repoutils.added-strings]
exclude = ["pyproject.toml"]

[[tool.repoutils.added-strings.strings]]
string = "FIXME"

[[tool.repoutils.added-strings.strings]]
regex = "skip: ?lint"
message = "please fix the lint instead"
include = ["*.py"]
exclude = ["vendor/*"]
"""


class _GitRepoTestCase(TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.invalid")
        self.write("pyproject.toml", _PYPROJECT)
        self.write("src/app.py", "a = 1\nb = 2  # FIXME\n")
        self.commit()
        self.git("checkout", "-qb", "feature")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout

    def write(self, path: str, content: str) -> None:
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)

    def commit(self, message: str = "commit") -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", message)


class AddedLinesTests(_GitRepoTestCase):
    def test_only_lines_added_since_the_merge_base(self) -> None:
        self.write("src/app.py", "a = 1\nx = 0\nb = 2  # FIXME\ny = 0\n")
        self.write("src/new.py", "one\ntwo\n")
        self.commit()
        # A later commit on main isn't part of the PR.
        self.git("checkout", "-q", "main")
        self.write("src/main_only.py", "FIXME\n")
        self.commit()
        self.git("checkout", "-q", "feature")
        # Working tree edits count too, but untracked files don't.
        self.write("src/new.py", "one\ntwo\nthree\n")
        self.write("untracked.py", "FIXME\n")

        self.assertEqual(
            list(added_lines(self.repo, "main")),
            [
                Location("src/app.py", 2, "x = 0"),
                Location("src/app.py", 4, "y = 0"),
                Location("src/new.py", 1, "one"),
                Location("src/new.py", 2, "two"),
                Location("src/new.py", 3, "three"),
            ],
        )

    def test_lines_that_look_like_diff_headers(self) -> None:
        # Added "++ b/x" shows as "+++ b/x", and removed "-- a/x" as "--- a/x".
        self.write("src/app.py", "-- a/old\n++ b/fake\n@@ -1 +1 @@\ndiff --git a b\n")
        self.commit()
        self.assertEqual(
            list(added_lines(self.repo, "main")),
            [
                Location("src/app.py", 1, "-- a/old"),
                Location("src/app.py", 2, "++ b/fake"),
                Location("src/app.py", 3, "@@ -1 +1 @@"),
                Location("src/app.py", 4, "diff --git a b"),
            ],
        )

    def test_renames_deletions_and_odd_paths(self) -> None:
        self.write("src/moved.py", "".join(f"line {i}\n" for i in range(20)))
        self.write("gone.py", "x\n")
        self.write("no_newline.py", "first")
        self.commit()
        base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "src/moved.py", "src/renamed.py")
        self.write("src/renamed.py", "".join(f"line {i}\n" for i in range(20)) + "added\n")
        self.git("rm", "-q", "gone.py")
        self.write('dir/tab\there "quoted" é.py', "odd\n")
        self.write("crlf.py", "windows\r\n")
        self.write("no_newline.py", "changed\nlast")
        self.write("control\x01.py", "ctrl\n")
        (self.repo / "binary.bin").write_bytes(b"\0FIXME\0")
        self.commit()

        self.assertEqual(
            sorted(added_lines(self.repo, base), key=lambda loc: loc.path),
            [
                Location("control\x01.py", 1, "ctrl"),
                Location("crlf.py", 1, "windows"),
                Location('dir/tab\there "quoted" é.py', 1, "odd"),
                Location("no_newline.py", 1, "changed"),
                Location("no_newline.py", 2, "last"),
                Location("src/renamed.py", 21, "added"),
            ],
        )

    def test_bad_base_raises(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            list(added_lines(self.repo, "no-such-branch"))


class FindTests(_GitRepoTestCase):
    def test_finds_each_rule_where_it_applies(self) -> None:
        self.write("src/app.py", "a = 1\nb = 2  # FIXME\nc = 3  # FIXME\nd = 4  # skip:lint\n")
        self.write("src/other.py", "e = 5  # skip: lint FIXME\n")
        self.write("vendor/lib.py", "# skip: lint\n")
        self.write("notes.md", "skip: lint\n")
        self.write("pyproject.toml", _PYPROJECT + "# FIXME\n")
        self.commit()

        config = load_config(self.repo)
        fixme, skip = config.rules
        self.assertEqual(
            find(self.repo, config, "main"),
            [
                Finding(
                    fixme,
                    [
                        Location("src/app.py", 3, "c = 3  # FIXME"),
                        Location("src/other.py", 1, "e = 5  # skip: lint FIXME"),
                    ],
                ),
                Finding(
                    skip,
                    [
                        Location("src/app.py", 4, "d = 4  # skip:lint"),
                        Location("src/other.py", 1, "e = 5  # skip: lint FIXME"),
                    ],
                ),
            ],
        )

    def test_nothing_added(self) -> None:
        self.assertEqual(find(self.repo, load_config(self.repo), "main"), [])


class LoadConfigTests(_GitRepoTestCase):
    def test_reads_the_table(self) -> None:
        self.assertEqual(
            load_config(self.repo),
            Config(
                rules=[
                    Rule("FIXME", is_regex=False, message=None),
                    Rule(
                        "skip: ?lint",
                        is_regex=True,
                        message="please fix the lint instead",
                        include=["*.py"],
                        exclude=["vendor/*"],
                    ),
                ],
                exclude=["pyproject.toml"],
            ),
        )

    def test_no_table_means_no_rules(self) -> None:
        self.write("pyproject.toml", "[project]\nname = 'x'\n")
        self.assertEqual(load_config(self.repo), Config(rules=[]))

    def test_rejects_bad_tables(self) -> None:
        cases = {
            "strings = 1": "strings must be an array of tables",
            "strings = [1]": "strings[0] must be a table",
            "strings = [{}]": "strings[0] must have one of string or regex",
            "strings = [{ string = 'a', regex = 'b' }]": "must have one of string or regex",
            "strings = [{ string = '' }]": "strings[0] string must be a non-empty string",
            "strings = [{ regex = 1 }]": "strings[0] regex must be a non-empty string",
            "strings = [{ regex = '(' }]": "strings[0] regex '(' is invalid: missing ), unterminated subpattern",
            "strings = [{ string = 'a', message = '' }]": "strings[0] message must be a non-empty string",
            "strings = [{ string = 'a', include = 'x' }]": "strings[0] include must be a list of strings",
            "strings = [{ string = 'a', strng = 'b' }]": "strings[0] has unknown keys: strng",
            "exclude = [1]": "exclude must be a list of strings",
            "string = 'a'": "[tool.repoutils.added-strings] has unknown keys: string",
        }
        for toml, message in cases.items():
            with self.subTest(toml):
                self.write("pyproject.toml", f"[tool.repoutils.added-strings]\n{toml}\n")
                with self.assertRaisesRegex(ConfigError, re.escape(message)):
                    load_config(self.repo)


class RenderTests(TestCase):
    def test_lists_each_finding_with_links(self) -> None:
        plain = Rule("FIXME", is_regex=False, message=None)
        regex = Rule("skip: ?lint", is_regex=True, message=None)
        findings = [
            Finding(plain, [Location("src/a b.py", 2, "  b = 2  # FIXME  "), Location("README.md", 9, "   ")]),
            Finding(regex, [Location("src/c.py", 1, "# skip:lint")]),
        ]
        self.assertEqual(
            render(findings, base="origin/main", commit="abc", repo="o/r"),
            "## Strings to double check\n"
            "\n"
            "**For information only.** Keep each one that is actually needed, with no reply or explanation, "
            "and remove the rest.\n"
            "\n"
            "Lines added at abc, compared with `origin/main`.\n"
            "\n"
            "- **Warning:** PR adds `FIXME` (2 times): please double check if use of `FIXME` is actually necessary\n"
            "\n"
            "  - [`src/a b.py:2`](https://github.com/o/r/blob/abc/src/a%20b.py?plain=1#L2): `b = 2  # FIXME`\n"
            "  - [`README.md:9`](https://github.com/o/r/blob/abc/README.md?plain=1#L9)\n"
            "\n"
            "- **Warning:** PR adds lines matching `skip: ?lint` (once): "
            "please double check if each is actually necessary\n"
            "\n"
            "  - [`src/c.py:1`](https://github.com/o/r/blob/abc/src/c.py?plain=1#L1): `# skip:lint`\n",
        )

    def test_caps_the_locations_and_long_lines(self) -> None:
        rule = Rule("x", is_regex=False, message="go *away*")
        locations = [Location("a.py", n, "x" * 200) for n in range(1, 26)]
        body = render([Finding(rule, locations)], base="main", commit=None, repo=None)
        self.assertIn("- **Warning:** PR adds `x` (25 times): go *away*\n", body)
        self.assertIn("  - `a.py:20`: `" + "x" * 119 + "…`\n  - and 5 more\n", body)
        self.assertNotIn("a.py:21", body)
        self.assertNotIn("Lines added at", body)

    def test_nothing_found(self) -> None:
        self.assertEqual(
            render([], base="main", commit="abc", repo="o/r"),
            "## Strings to double check\n\n"
            "No added lines have any of the strings to double check, compared with `main`.\n",
        )

    def test_code_span(self) -> None:
        self.assertEqual(code_span("a"), "`a`")
        self.assertEqual(code_span("a`b"), "``a`b``")
        self.assertEqual(code_span("`a``"), "``` `a`` ```")


class FakeGitHub:
    """Answers github_comments.request for PR 7 of o/r, with head "abc123" and
    the given issue comments."""

    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self.comments = comments
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, url: str, token: str, *, json_body: Any = None, **kwargs: object) -> Any:
        self.calls.append((method, url, json_body))
        if url.endswith("/pulls/7"):
            return {"head": {"sha": "abc123"}}
        if url.endswith("/user"):
            return {"login": "me"}
        if method == "GET" and "/issues/7/comments" in url:
            return self.comments
        return {}

    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] != "GET"]


class MainTests(_GitRepoTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.write("src/app.py", "a = 1\nb = 2  # FIXME\nc = 3  # FIXME\n")
        self.commit()

    def _run(self, argv: list[str], request: Any = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {"GITHUB_TOKEN": "tok", "GITHUB_REPOSITORY": "o/r"}),
            mock.patch.object(github_comments, "request", request or FakeGitHub([])),
            mock.patch.object(Path, "cwd", return_value=self.repo),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = added_strings.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_prints_report_and_exits_zero(self) -> None:
        head = self.git("rev-parse", "HEAD").strip()
        code, out, err = self._run(["--base", "main"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(
            f"- [`src/app.py:3`](https://github.com/o/r/blob/{head}/src/app.py?plain=1#L3): `c = 3  # FIXME`", out
        )

    def test_posts_new_comment(self) -> None:
        gh = FakeGitHub([{"id": 1, "user": {"login": "someone"}, "body": MARKER + "\nold"}])
        code, out, _ = self._run(["--base", "main", "--commit", "abc", "--post", "7", "--author", "bot"], gh)
        self.assertEqual(code, 0)
        self.assertIn("Posted the added strings report to PR #7.", out)
        [(method, url, body)] = gh.writes()
        self.assertEqual((method, url), ("POST", f"{github_comments.API_URL}/repos/o/r/issues/7/comments"))
        self.assertTrue(body["body"].startswith(MARKER + "\n## Strings to double check"))

    def test_looks_up_author_and_uses_the_configured_prefix(self) -> None:
        self.write("pyproject.toml", '[tool.repoutils]\ncomment-prefix = "proj"\n' + _PYPROJECT)
        gh = FakeGitHub([{"id": 3, "user": {"login": "me"}, "body": "<!-- proj-added-strings -->\nold"}])
        code, _, _ = self._run(["--base", "main", "--post", "7"], gh)
        self.assertEqual(code, 0)
        self.assertEqual(
            [(m, u) for m, u, _ in gh.writes()], [("PATCH", f"{github_comments.API_URL}/repos/o/r/issues/comments/3")]
        )

    def test_stale_commit_posts_nothing(self) -> None:
        gh = FakeGitHub([])
        code, out, _ = self._run(["--base", "main", "--commit", "def", "--post", "7", "--author", "bot"], gh)
        self.assertEqual(code, 0)
        self.assertIn("Not posting: the report is for def, but PR #7's head is now abc123.", out)
        self.assertEqual(gh.writes(), [])

    def test_nothing_found_only_replaces_an_earlier_report(self) -> None:
        self.write("src/app.py", "a = 1\nb = 2  # FIXME\n")
        self.commit()
        with self.subTest("no earlier report"):
            gh = FakeGitHub([])
            code, out, _ = self._run(["--base", "main", "--post", "7", "--author", "bot"], gh)
            self.assertEqual(code, 0)
            self.assertIn("Not posting: PR #7 adds none of the strings, and has no earlier report.", out)
            self.assertEqual(gh.writes(), [])
        with self.subTest("earlier report"):
            gh = FakeGitHub([{"id": 3, "user": {"login": "bot"}, "body": MARKER + "\nold"}])
            code, _, _ = self._run(["--base", "main", "--post", "7", "--author", "bot"], gh)
            self.assertEqual(code, 0)
            [(method, _, body)] = gh.writes()
            self.assertEqual(method, "PATCH")
            self.assertIn("No added lines have any of the strings", body["body"])

    def test_post_looks_up_repo_for_links(self) -> None:
        gh = FakeGitHub([])
        with (
            mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": ""}),
            mock.patch.object(github_comments, "gh_output", return_value="o/r") as gh_output,
        ):
            code, _, _ = self._run(["--base", "main", "--repo", "", "--post", "7", "--author", "bot"], gh)
        self.assertEqual(code, 0)
        gh_output.assert_called_once_with("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        [(_, url, _)] = gh.writes()
        self.assertEqual(url, f"{github_comments.API_URL}/repos/o/r/issues/7/comments")

    def test_base_defaults_to_the_pr_base_then_the_default_branch(self) -> None:
        for base_ref, expected in (("release", "origin/release"), ("", "origin/main")):
            with self.subTest(base_ref=base_ref), mock.patch.dict("os.environ", {"GITHUB_BASE_REF": base_ref}):
                code, _, err = self._run([])
                self.assertEqual(code, 1)
                self.assertIn(f"git merge-base {expected} HEAD failed", err)

    def test_bad_config_fails(self) -> None:
        self.write("pyproject.toml", "[tool.repoutils.added-strings]\nstrings = [{ regex = '(' }]\n")
        code, _, err = self._run(["--base", "main"])
        self.assertEqual(code, 1)
        self.assertIn("regex '(' is invalid", err)

    def test_http_error_fails(self) -> None:
        def request(*args: object, **kwargs: object) -> Any:
            raise urllib.error.HTTPError("https://x", 403, "Forbidden", Message(), io.BytesIO(b"nope"))

        code, _, err = self._run(["--base", "main", "--post", "7", "--author", "bot"], request)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403 from https://x:\nnope", err)


class OwnConfigTests(TestCase):
    """The regexes repoutils' own pyproject.toml gives, which match only code
    and comments, not strings or test data that name them."""

    @override
    def setUp(self) -> None:
        self.type_checking, self.noqa = load_config(Path(__file__).parent.parent).rules

    def test_type_checking(self) -> None:
        for line in ("if TYPE_CHECKING:", "    if TYPE_CHECKING:", "if typing.TYPE_CHECKING:"):
            with self.subTest(line):
                self.assertTrue(self.type_checking.matches(line))
        for line in ('x = "if TYPE_CHECKING:"', "# if TYPE_CHECKING:", "if TYPE_CHECKING_X:"):
            with self.subTest(line):
                self.assertFalse(self.type_checking.matches(line))

    def test_noqa(self) -> None:
        for line in (
            "import x  # noqa: F401",
            "import x  # noqa",
            "import x  #noqa:F401",
            "import x  # NOQA: F401",
            'x = "a#b"  # noqa: E501',
            "x = 'it\\'s'  # noqa: E501",
            'x = f"{y}" + "z"  # noqa: E501',
        ):
            with self.subTest(line):
                self.assertTrue(self.noqa.matches(line))
        for line in (
            'x = "# noqa: E501"',
            "x = '# noqa'",
            'x = "a \\" # noqa"',
            '"""Docs that mention # noqa."""',
            "x = 1  # see noqa docs",
            "# noqanot",
        ):
            with self.subTest(line):
                self.assertFalse(self.noqa.matches(line))

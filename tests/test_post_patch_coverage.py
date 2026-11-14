import contextlib
import io
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from email.message import Message
from pathlib import Path
from typing import Any
from unittest import TestCase, mock

from repoutils.commands.post_patch_coverage import MARKER as PATCH_COVERAGE_MARKER
from repoutils.commands.post_patch_coverage import MAX_BODY_CHARS, ProjectCoverage, _build_body
from repoutils.commands.post_patch_coverage import main as post_patch_coverage_main
from repoutils.commands.post_screenshots import MARKER
from repoutils.coverage_status import Totals


class PostPatchCoverageTests(TestCase):
    HEAD = (
        "# Diff Coverage\n## Diff: origin/master...HEAD\n\n- a&#46;py (50.0%): Missing lines 2\n\n"
        "## Summary\n\n- **Total**: 2 lines\n- **Missing**: 1 line\n- **Coverage**: 50%\n"
    )
    REPORT = HEAD + "\n\n\n## a&#46;py\n\nLines 1-2\n\n```python\n  1 a = 1\n! 2 b = 2\n```\n\n\n---\n"

    def _run(
        self,
        *args: str,
        report: str | None = REPORT,
        comments: Sequence[dict[str, Any]] = (),
        fail: tuple[Any, ...] | None = None,
        head: str = "abc1234def",
        statuses: Sequence[dict[str, Any]] = (),
        files: dict[str, str] | None = None,
        pr: tuple[str, ...] = ("7",),
    ) -> tuple[int, str, list[tuple[Any, ...]]]:
        """Run main() on PR 7 (or pr) in a temp dir holding patch-coverage.md
        (unless report is None) and files, with the API faked as in
        PostScreenshotsTests and each commit having statuses. Return the exit
        code, stderr and each request as (method, url, token, json_body), and
        keep stdout in self.stdout and the files written in self.files."""
        requests: list[tuple[Any, ...]] = []

        def fake_request(method: str, url: str, token: str, *, json_body: object = None, **kw: Any) -> Any:
            requests.append((method, url, token, json_body))
            if fail and method == fail[0] and url.startswith(fail[1]):
                if len(fail) > 2:
                    raise fail[2]
                raise urllib.error.HTTPError(url, 403, "Forbidden", Message(), io.BytesIO(b"nope"))
            if url == "https://api.github.com/user":
                return {"login": "me"}
            if url == "https://api.github.com/repos/o/r/pulls/7":
                return {"head": {"sha": head}}
            if method == "GET" and "/issues/7/comments" in url:
                return list(comments)
            if method == "GET" and "/status?" in url:
                return {"statuses": list(statuses)}
            return None

        with tempfile.TemporaryDirectory() as tmp:
            if report is not None:
                (Path(tmp) / "patch-coverage.md").write_text(report)
            for name, text in (files or {}).items():
                (Path(tmp) / name).write_text(text)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                contextlib.chdir(tmp),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                mock.patch.dict("os.environ", {"GITHUB_TOKEN": "ghs", "GITHUB_REPOSITORY": "o/r"}, clear=True),
                mock.patch("repoutils.github.request", fake_request),
            ):
                code = post_patch_coverage_main([*pr, *args])
            self.files = {path.name: path.read_text() for path in Path(tmp).iterdir()}
        self.stdout = stdout.getvalue()
        return code, stderr.getvalue(), requests

    def _writes(self, requests: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [r for r in requests if r[0] in ("POST", "PATCH", "DELETE")]

    def test_posts_report(self) -> None:
        code, _, requests = self._run(
            "--commit",
            "abc1234",
            "--run-url",
            "https://example.com/run",
            "--author",
            "github-actions[bot]",
        )
        self.assertEqual(code, 0)
        self.assertEqual({r[2] for r in requests}, {"ghs"})
        self.assertNotIn("https://api.github.com/user", [r[1] for r in requests])
        (post,) = self._writes(requests)
        self.assertEqual(post[:2], ("POST", "https://api.github.com/repos/o/r/issues/7/comments"))
        self.assertEqual(
            post[3]["body"],
            f"{PATCH_COVERAGE_MARKER}\n## Diff Coverage\n\n"
            "Patch coverage of abc1234, from [this workflow run](https://example.com/run).\n\n"
            "### Diff: origin/master...HEAD\n\n"
            "| File | Coverage | Missing lines |\n| --- | ---: | --- |\n| `a.py` | 50.0% | 2 |\n\n"
            "### Summary\n\n| Total | Missing | Coverage |\n| ---: | ---: | ---: |\n| 2 lines | 1 line | 50% |\n\n\n\n"
            "### a&#46;py\n\nLines 1-2\n\n```python\n  1 a = 1\n! 2 b = 2\n```\n\n\n---",
        )

    def test_no_lines(self) -> None:
        report = "# Diff Coverage\n## Diff: origin/master...HEAD\n\nNo lines with coverage information in this diff.\n"
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        self.assertEqual(
            self._writes(requests)[0][3]["body"],
            f"{PATCH_COVERAGE_MARKER}\n## Diff Coverage\n\n### Diff: origin/master...HEAD\n\n"
            "No lines with coverage information in this diff.",
        )

    def test_unexpected_report_fails(self) -> None:
        no_lines = "# Diff Coverage\n## Diff: x\n\nNo lines with coverage information in this diff.\n"
        cases = {
            "": "line 1: expected '# Diff Coverage', found ''",
            "# Coverage\n": "line 1: expected '# Diff Coverage'",
            "# Diff Coverage\n": "line 2: expected '## Diff: ...', found the end of the report",
            "# Diff Coverage\n## Diff: x\n\n* a.py (50%)\n": "line 4: expected a file's coverage",
            "# Diff Coverage\n## Diff: x\n\n- a.py (50%): Missing 2\n": "line 4: expected a file's coverage",
            no_lines + "more\n": "line 5: expected nothing more, found 'more'",
            self.HEAD.replace("## Summary", "## Totals"): "line 6: expected '## Summary', found '## Totals'",
            self.HEAD.replace("**Missing**", "**Uncovered**"): "line 9: expected '- **Missing**: ...'",
            self.HEAD.replace("**Total**: 2 lines", "**Coverage**: 50%"): "line 8: expected '- **Total**: ...'",
            self.HEAD.replace("- **Coverage**: 50%\n", ""): "line 10: expected '- **Coverage**: ...', found the end",
            self.HEAD.replace("2 lines", "two lines"): "line 8: expected '<n> lines', found 'two lines'",
            self.HEAD.replace("2 lines", "0 lines"): "line 9: 1 missing lines of 0",
        }
        for report, error in cases.items():
            with self.subTest(report):
                code, stderr, requests = self._run(report=report)
                self.assertEqual(code, 1)
                self.assertIn(f"diff-cover report {error}", stderr)
                self.assertEqual(requests, [])

    def test_stale_commit_posts_nothing(self) -> None:
        code, _, requests = self._run("--commit", "0ld0ld0")
        self.assertEqual(code, 0)
        self.assertEqual(self._writes(requests), [])

    def test_edits_previous_comment(self) -> None:
        comments = [
            {"id": 1, "user": {"login": "me"}, "body": MARKER + "\nscreenshots"},
            {"id": 2, "user": {"login": "me"}, "body": PATCH_COVERAGE_MARKER + "\nold"},
        ]
        code, _, requests = self._run(comments=comments)
        self.assertEqual(code, 0)
        self.assertEqual(
            [r[:2] for r in self._writes(requests)],
            [("PATCH", "https://api.github.com/repos/o/r/issues/comments/2")],
        )

    def test_lists_become_tables(self) -> None:
        report = (
            "# Diff Coverage\n## Diff: origin/master...HEAD\n\n"
            "- scripts/a&#46;py (100%)\n- b&#46;py (40.0%): Missing lines 3,5-9,12\n\n"
            "## Summary\n\n- **Total**: 10 lines\n- **Missing**: 6 lines\n- **Coverage**: 40%\n\n"
            "## b&#46;py\n\n```python\n# not a heading\n```\n- something else\n"
        )
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        self.assertEqual(
            self._writes(requests)[0][3]["body"].split("\n")[3:],
            [
                "### Diff: origin/master...HEAD",
                "",
                "| File | Coverage | Missing lines |",
                "| --- | ---: | --- |",
                "| `scripts/a.py` | 100% |  |",
                "| `b.py` | 40.0% | 3, 5-9, 12 |",
                "",
                "### Summary",
                "",
                "| Total | Missing | Coverage |",
                "| ---: | ---: | ---: |",
                "| 10 lines | 6 lines | 40% |",
                "",
                "### b&#46;py",
                "",
                "```python",
                "# not a heading",
                "```",
                "- something else",
            ],
        )

    def test_awkward_path(self) -> None:
        # A pipe would end the cell, and a backtick the code span.
        report = self.HEAD.replace("- a&#46;py (50.0%)", "- a|b`c&#46;py (50.0%)")
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        self.assertIn("| `` a\\|b`c.py `` | 50.0% | 2 |", self._writes(requests)[0][3]["body"])

    def test_backslash_before_pipe(self) -> None:
        # GFM's table extension takes the backslash right before a pipe as its
        # escape and drops only that one, so the path's own backslash survives.
        report = self.HEAD.replace("- a&#46;py (50.0%)", "- a\\|b&#46;py (50.0%)")
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        self.assertIn("| `a\\\\|b.py` | 50.0% | 2 |", self._writes(requests)[0][3]["body"])

    def test_long_report_truncated(self) -> None:
        lines = "\n".join(f"  {n} x = {n}" for n in range(20_000))
        report = f"{self.HEAD}\n## a.py\n\n```python\n{lines}\n```\n"
        code, _, requests = self._run("--run-url", "https://example.com/run", report=report)
        self.assertEqual(code, 0)
        body = self._writes(requests)[0][3]["body"]
        self.assertLess(len(body), 65_536)
        before, note = body.rsplit("\n\n", 1)
        self.assertLessEqual(len(before), MAX_BODY_CHARS + 4)
        self.assertTrue(before.endswith("\n```"))
        self.assertEqual(before.count("```"), 2)
        self.assertIn("| 2 lines | 1 line | 50% |", before)
        self.assertEqual(
            note,
            "_Report truncated; the whole of it is in the `patch-coverage` artifact of "
            "[this workflow run](https://example.com/run)._",
        )

    def test_long_report_truncated_outside_code(self) -> None:
        # A cut in the file table, outside any code block, adds no fence.
        files = "".join(f"- f{n}&#46;py (50.0%): Missing lines 2\n" for n in range(5_000))
        report = self.HEAD.replace("- a&#46;py (50.0%): Missing lines 2\n", files)
        code, _, requests = self._run(report=report)
        self.assertEqual(code, 0)
        before, note = self._writes(requests)[0][3]["body"].rsplit("\n\n", 1)
        self.assertLessEqual(len(before), MAX_BODY_CHARS)
        self.assertNotIn("```", before)
        self.assertTrue(before.endswith(" | 50.0% | 2 |"))
        self.assertEqual(
            note, "_Report truncated; the whole of it is in the `patch-coverage` artifact of the workflow run._"
        )

    BASE = "1a2b3c4" + "0" * 33
    COVERAGE_XML = '<coverage lines-valid="100" lines-covered="95" />'

    def _project(
        self, *args: str, base_description: str | None = "96.00% (96/100 lines)", pyproject: str = "", **kw: Any
    ) -> tuple[int, str, list[tuple[Any, ...]]]:
        """_run with coverage.xml at 95% and the base's coverage/project status giving base_description."""
        statuses = [{"context": "lint", "description": "ok"}]
        if base_description is not None:
            statuses.append({"context": "coverage/project", "description": base_description})
        files = {"coverage.xml": self.COVERAGE_XML, "pyproject.toml": pyproject}
        return self._run(
            "--coverage-report", "coverage.xml", "--base", self.BASE, *args, statuses=statuses, files=files, **kw
        )

    def test_project_coverage_and_change(self) -> None:
        code, _, requests = self._project("--commit", "abc1234")
        self.assertEqual(code, 0)
        self.assertIn(
            ("GET", f"https://api.github.com/repos/o/r/commits/{self.BASE}/status?per_page=100&page=1"),
            [r[:2] for r in requests],
        )
        warning = (
            "Patch coverage is 50.00%, 46.00 points below the base's project coverage of 96.00%; "
            "`warn-patch-below` allows 1."
        )
        self.assertEqual(
            self._writes(requests)[0][3]["body"].split("### Diff:")[0],
            f"{PATCH_COVERAGE_MARKER}\n## Coverage\n\nCoverage of abc1234.\n\n"
            f"> [!WARNING]\n> {warning}\n\n"
            "### Project\n\n| Commit | Coverage |\n| --- | ---: |\n"
            "| Head, `abc1234` | 95.00% (95/100 lines) |\n"
            "| Base, `1a2b3c4` | 96.00% (96/100 lines) |\n"
            "| Change | -1.00 points |\n\n",
        )
        self.assertIn(f"::warning::{warning}\n", self.stdout)

    def test_project_drop_warning(self) -> None:
        code, _, requests = self._project(base_description="97.50% (195/200 lines), +0.10 points from 0123456")
        self.assertEqual(code, 0)
        body = self._writes(requests)[0][3]["body"]
        self.assertIn("> Project coverage fell by 2.50 points from the base's; `warn-project-drop` allows 2.\n", body)
        self.assertIn("| Change | -2.50 points |", body)
        self.assertIn("::warning::Project coverage fell by 2.50 points", self.stdout)

    def test_project_rise(self) -> None:
        code, _, requests = self._project(base_description="40.00% (40/100 lines)")
        self.assertEqual(code, 0)
        body = self._writes(requests)[0][3]["body"]
        self.assertIn("| Change | +55.00 points |", body)
        self.assertNotIn("WARNING", body)
        self.assertNotIn("::warning::", self.stdout)

    def test_configured_limits(self) -> None:
        pyproject = "[tool.repoutils.coverage]\nwarn-project-drop = 3\nwarn-patch-below = false\n"
        code, _, requests = self._project(base_description="97.50% (195/200 lines)", pyproject=pyproject)
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", self._writes(requests)[0][3]["body"])
        pyproject = "[tool.repoutils.coverage]\nwarn-project-drop = 2.4\nwarn-patch-below = 50\n"
        code, _, requests = self._project(base_description="97.50% (195/200 lines)", pyproject=pyproject)
        self.assertEqual(code, 0)
        body = self._writes(requests)[0][3]["body"]
        self.assertIn("`warn-project-drop` allows 2.4.", body)
        self.assertNotIn("warn-patch-below", body)

    def test_bad_limit_fails(self) -> None:
        code, stderr, requests = self._project(pyproject="[tool.repoutils.coverage]\nwarn-patch-below = -1\n")
        self.assertEqual(code, 1)
        self.assertIn("[tool.repoutils.coverage] warn-patch-below must be a number, 0 or more, or false", stderr)
        self.assertEqual(requests, [])

    def test_bad_coverage_report_fails(self) -> None:
        code, stderr, requests = self._run("--coverage-report", "coverage.xml", files={"coverage.xml": "<coverage/>"})
        self.assertEqual(code, 1)
        self.assertIn("coverage.xml: not a Cobertura coverage report", stderr)
        self.assertEqual(requests, [])

    def test_no_base_status(self) -> None:
        code, _, requests = self._project("--commit", "abc1234", base_description=None)
        self.assertEqual(code, 0)
        body = self._writes(requests)[0][3]["body"]
        self.assertIn(
            "| Head, `abc1234` | 95.00% (95/100 lines) |\n\n"
            "The base commit, `1a2b3c4`, has no `coverage/project` status to compare with, "
            "as its CI failed or didn't finish in time.\n\n### Diff:",
            body,
        )
        # The patch is compared with the PR's own project coverage instead.
        self.assertIn("50.00%, 45.00 points below the PR's project coverage of 95.00%", body)

    def test_bad_base_status_fails(self) -> None:
        code, stderr, requests = self._project(base_description="lots")
        self.assertEqual(code, 1)
        self.assertIn("not a coverage status description: 'lots'", stderr)
        self.assertEqual(self._writes(requests), [])

    def test_without_base(self) -> None:
        code, _, requests = self._run("--coverage-report", "coverage.xml", files={"coverage.xml": self.COVERAGE_XML})
        self.assertEqual(code, 0)
        self.assertNotIn("/status?", "".join(r[1] for r in requests))
        body = self._writes(requests)[0][3]["body"]
        self.assertIn("| Head | 95.00% (95/100 lines) |\n\n### Diff:", body)
        self.assertIn("below the PR's project coverage of 95.00%", body)

    def test_output(self) -> None:
        code, _, requests = self._run(
            "--output",
            "summary.md",
            "--coverage-report",
            "coverage.xml",
            files={"coverage.xml": self.COVERAGE_XML, "summary.md": "before\n"},
            pr=(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(requests, [])
        summary = self.files["summary.md"]
        self.assertTrue(summary.startswith("before\n## Coverage\n\n> [!WARNING]\n"))
        self.assertNotIn("<!--", summary)
        self.assertTrue(summary.endswith("```\n\n\n---\n"))
        self.assertIn("::warning::Patch coverage is 50.00%", self.stdout)

    def test_output_with_base(self) -> None:
        code, _, requests = self._project("--output", "summary.md", pr=())
        self.assertEqual(code, 0)
        self.assertEqual([r[0] for r in requests], ["GET"])
        self.assertIn("| Base, `1a2b3c4` | 96.00% (96/100 lines) |", self.files["summary.md"])

    def test_output_unwritable(self) -> None:
        code, stderr, _ = self._run("--output", "missing/summary.md", pr=())
        self.assertEqual(code, 1)
        self.assertIn("missing/summary.md", stderr)

    def test_usage_errors(self) -> None:
        for args, error in [
            ((), "give a PR number, or --output"),
            (("7", "--base", "abc"), "--base needs --coverage-report"),
        ]:
            with self.subTest(args), contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    post_patch_coverage_main(list(args))
                self.assertIn(error, stderr.getvalue())

    def test_missing_report(self) -> None:
        code, stderr, requests = self._run(report=None)
        self.assertEqual(code, 1)
        self.assertIn("patch-coverage.md", stderr)
        self.assertEqual(requests, [])

    def test_network_error_reported(self) -> None:
        code, stderr, _ = self._run(fail=("GET", "https://api.github.com/user", urllib.error.URLError("unreachable")))
        self.assertEqual(code, 1)
        self.assertIn("unreachable", stderr)

    def test_http_error_reported(self) -> None:
        code, stderr, _ = self._run(fail=("POST", "https://api.github.com/repos/o/r/issues/7/comments"))
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", stderr)
        self.assertIn("nope", stderr)

    def test_gh_failure_reported(self) -> None:
        def fake_gh(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, cmd, stderr="not logged in")

        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("repoutils.github.subprocess.run", fake_gh),
            tempfile.TemporaryDirectory() as tmp,
            contextlib.chdir(tmp),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            Path("patch-coverage.md").write_text(self.REPORT)
            code = post_patch_coverage_main(["7"])
        self.assertEqual(code, 1)
        self.assertIn("not logged in", stderr.getvalue())


class PatchCoverageRealDiffCoverTests(TestCase):
    """post_patch_coverage on reports from the installed diff-cover, so a
    diff-cover upgrade (e.g. Dependabot's) that changes the Markdown format
    the script expects fails here, rather than only in post-patch-coverage,
    which doesn't run on Dependabot PRs."""

    def _report(self, new_source: str, hits: dict[int, int]) -> str:
        """diff-cover's Markdown report for a branch that changes a.py from
        one line to new_source, with coverage.xml giving each line's hits."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                    cwd=path,
                    check=True,
                    capture_output=True,
                )

            git("init", "-q", "-b", "main")
            (path / "a.py").write_text("a = 1\n")
            git("add", "a.py")
            git("commit", "-qm", "base")
            git("checkout", "-qb", "change")
            (path / "a.py").write_text(new_source)
            git("commit", "-qam", "change")
            lines = "".join(f'<line number="{n}" hits="{h}"/>' for n, h in hits.items())
            (path / "coverage.xml").write_text(
                '<?xml version="1.0" ?><coverage version="7"><sources><source>.</source></sources>'
                '<packages><package name="."><classes><class name="a.py" filename="a.py">'
                f"<lines>{lines}</lines></class></classes></package></packages></coverage>"
            )
            subprocess.run(
                [
                    Path(sys.executable).parent / "diff-cover",
                    "coverage.xml",
                    "--compare-branch=main",
                    "--format",
                    "markdown:report.md",
                ],
                cwd=path,
                check=True,
                capture_output=True,
            )
            return (path / "report.md").read_text()

    def test_missing_lines(self) -> None:
        project = ProjectCoverage(Totals(90, 100))
        body, warnings = _build_body(
            self._report("a = 1\nb = 2\nc = 3\nd = 4\n", {1: 1, 2: 0, 3: 1, 4: 0}), "abc", None, project=project
        )
        # The patch figure in the warning is from the summary's line counts.
        self.assertEqual(
            warnings,
            [
                "Patch coverage is 33.33%, 56.67 points below the PR's project coverage of 90.00%; "
                "`warn-patch-below` allows 1."
            ],
        )
        self.assertIn("| `a.py` | 33.3% | 2, 4 |", body)
        self.assertIn("| 3 lines | 2 lines | 33% |", body)
        self.assertIn("! 2 b = 2", body)

    def test_all_covered(self) -> None:
        body, warnings = _build_body(
            self._report("a = 1\nb = 2\n", {1: 1, 2: 1}), "abc", None, project=ProjectCoverage(Totals(100, 100))
        )
        self.assertEqual(warnings, [])
        self.assertIn("| `a.py` | 100% |  |", body)
        self.assertIn("| 1 line | 0 lines | 100% |", body)

    def test_no_lines(self) -> None:
        body, warnings = _build_body(
            self._report("a = 1\n# comment\n", {1: 1}), "abc", None, project=ProjectCoverage(Totals(0, 100))
        )
        self.assertEqual(warnings, [])
        self.assertTrue(body.endswith("No lines with coverage information in this diff."))

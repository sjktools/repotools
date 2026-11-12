"""Check Markdown prose for long, clause-heavy sentences, or too many short ones.

The files are ``[tool.repoutils.prose]``'s ``files`` (default ``AGENTS.md``
and ``README.md``), or the ones given; a missing configured file is skipped.

It measures each file's prose sentences (see ``repoutils.markdown``) and fails
a file whose rate is over a limit. The limits are on a whole file's rates and
not on each sentence, and the worst sentences are listed to show where to start.

The default limits sit between READMEs people wrote before language models and
documents that read as machine-written: those have much longer sentences, with
more commas, semicolons and joined clauses.

Every one of those limits is a ceiling, and prose rewritten only to get under
them ends up as a run of very short sentences, which reads no better. So one
more limit caps the share of short sentences. It leaves list items out, where
a terse sentence is at home, and sentences that end in a colon, which lead
into a list or a code block.
"""

import argparse
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from repoutils import config
from repoutils.markdown import Sentence, prose_sentences, words

WHERE = "[tool.repoutils.prose]"
DEFAULT_FILES = ["AGENTS.md", "README.md"]
DEFAULT_MIN_SENTENCES = 15
DEFAULT_LONG_SENTENCE_WORDS = 30
DEFAULT_SHORT_SENTENCE_WORDS = 8
# How many of a file's worst sentences to list under each rate that's over its limit.
HINTS = 5
SNIPPET = 80

# Where one clause is joined to the next: a comma and a conjunction, a semicolon, a colon or a dash.
CONJUNCTIONS = (
    "and",
    "or",
    "but",
    "so",
    "yet",
    "which",
    "where",
    "while",
    "though",
    "although",
    "whereas",
    "because",
    "since",
    "unless",
    "however",
)
JOINT_RE = re.compile(r",\s+(?:" + "|".join(CONJUNCTIONS) + r")\b|;|:\s|\s(?:[—–-]|--)\s|—")


@dataclass(frozen=True)
class Metric:
    """A rate over a file's sentences, and the config key of its limit."""

    key: str
    default: float
    name: str
    # What one sentence adds to the rate.
    count: Callable[[str], int]
    unit: str
    # mean: the count per sentence. share: the percentage of sentences that count.
    kind: Literal["mean", "share"] = "mean"
    # Whether the worst sentences are the ones with the lowest counts, not the highest.
    lowest_first: bool = False
    # Whether the rate leaves out the sentences in list items, and the ones that
    # end in a colon, which lead into a list or a code block.
    skip_terse: bool = False

    def counted(self, sentences: list[Sentence]) -> list[tuple[int, Sentence]]:
        """What each sentence the rate covers adds to it."""
        return [
            (self.count(sentence.text), sentence)
            for sentence in sentences
            if not (self.skip_terse and (sentence.in_list or sentence.text.endswith(":")))
        ]

    def value(self, sentences: list[Sentence]) -> float:
        counts = [count for count, _ in self.counted(sentences)]
        if not counts:
            return 0
        if self.kind == "share":
            return 100 * sum(count > 0 for count in counts) / len(counts)
        return sum(counts) / len(counts)

    def over(self, sentences: list[Sentence], limit: float | None) -> bool:
        return limit is not None and self.value(sentences) > limit


def _word_count(text: str) -> int:
    return len(words(text))


def metrics(
    long_sentence_words: int = DEFAULT_LONG_SENTENCE_WORDS, short_sentence_words: int = DEFAULT_SHORT_SENTENCE_WORDS
) -> list[Metric]:
    """The rates, in the order they're reported."""

    def long_words(text: str) -> int:
        count = _word_count(text)
        return count if count > long_sentence_words else 0

    def short_words(text: str) -> int:
        count = _word_count(text)
        return count if count < short_sentence_words else 0

    return [
        Metric("max-words-per-sentence", 18, "words per sentence", _word_count, "words"),
        Metric(
            "max-long-sentences", 12, f"% of sentences over {long_sentence_words} words", long_words, "words", "share"
        ),
        Metric(
            "max-short-sentences",
            25,
            f"% of sentences outside lists under {short_sentence_words} words",
            short_words,
            "words",
            "share",
            lowest_first=True,
            skip_terse=True,
        ),
        Metric("max-commas-per-sentence", 1.3, "commas per sentence", lambda text: text.count(","), "commas"),
        Metric(
            "max-semicolons-per-sentence", 0.05, "semicolons per sentence", lambda text: text.count(";"), "semicolons"
        ),
        Metric(
            "max-joints-per-sentence",
            0.6,
            "clause joints per sentence",
            lambda text: len(JOINT_RE.findall(text)),
            "clause joints",
        ),
    ]


def files() -> list[str]:
    """The files to check, from config."""
    return config.string_list(config.section("prose"), "files", DEFAULT_FILES, WHERE)


def _snippet(text: str) -> str:
    return text if len(text) <= SNIPPET else text[: SNIPPET - 1].rstrip() + "…"


def _rate(metric: Metric, sentences: list[Sentence], limit: float | None) -> str:
    note = "no limit" if limit is None else f"limit {limit:g}"
    return f"- {metric.value(sentences):.2f} {metric.name} ({note})"


def _report(
    path: str, sentences: list[Sentence], limits: list[tuple[Metric, float | None]], *, show_stats: bool
) -> bool:
    """Print the file's rates that are over their limits (or all of them), and return whether any is."""
    lines: list[str] = []
    failed = False
    hinted: set[tuple[str, bool]] = set()
    for metric, limit in limits:
        over = metric.over(sentences, limit)
        failed = failed or over
        if not over and not show_stats:
            continue
        lines.append(_rate(metric, sentences, limit) + (" <- over" if over and show_stats else ""))
        # Two rates can count the same thing (words); list its worst sentences once.
        if over and (metric.unit, metric.lowest_first) not in hinted:
            hinted.add((metric.unit, metric.lowest_first))
            sign = 1 if metric.lowest_first else -1
            counted = metric.counted(sentences)
            worst = sorted((pair for pair in counted if pair[0]), key=lambda pair: sign * pair[0])[:HINTS]
            for count, sentence in worst:
                unit = metric.unit if count > 1 else metric.unit[:-1]
                lines.append(f"  - {path}:{sentence.line}: {count} {unit}: {_snippet(sentence.text)}")
    verdict = "over the limits" if failed else "within the limits"
    print(f"{path}: {len(sentences)} sentences, {verdict}{':' if lines else '.'}")
    for line in lines:
        print(line)
    return failed


def main(paths: list[str] | None = None, *, exit_zero: bool = False, show_stats: bool = False) -> int:
    table = config.section("prose")
    min_sentences = config.integer(table, "min-sentences", DEFAULT_MIN_SENTENCES, WHERE)
    long_sentence_words = config.integer(table, "long-sentence-words", DEFAULT_LONG_SENTENCE_WORDS, WHERE)
    short_sentence_words = config.integer(table, "short-sentence-words", DEFAULT_SHORT_SENTENCE_WORDS, WHERE)
    limits = [
        (metric, config.optional_number(table, metric.key, metric.default, WHERE))
        for metric in metrics(long_sentence_words, short_sentence_words)
    ]
    if paths is None:
        paths = [path for path in files() if Path(path).is_file()]
    failed = []
    for path in paths:
        sentences = prose_sentences(Path(path).read_text(encoding="utf-8"))
        # No sentences means no rates, whatever min-sentences is.
        if len(sentences) < max(min_sentences, 1):
            # Its rates are still worth seeing, though no limit applies to them.
            rates = [_rate(metric, sentences, limit) for metric, limit in limits] if show_stats and sentences else []
            end = ":" if rates else "."
            print(f"{path}: {len(sentences)} sentences, too few to judge (min-sentences is {min_sentences}){end}")
            for rate in rates:
                print(rate)
        elif _report(path, sentences, limits, show_stats=show_stats):
            failed.append(path)
    if not paths:
        print("No files to check.")
    if failed:
        print(f"Prose check failed: {', '.join(failed)}.")
        return 0 if exit_zero else 1
    return 0


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="repoutils check-prose", description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", metavar="FILE", help=f"files to check (default: {WHERE}'s files)")
    parser.add_argument("--exit-zero", action="store_true", help="report, but exit 0 even if a file is over a limit")
    parser.add_argument("--stats", action="store_true", help="print every rate for every file, not only those over")
    args = parser.parse_args(argv)
    for path in args.files:
        if not Path(path).is_file():
            parser.error(f"{path} is not a file")
    return main(args.files or None, exit_zero=args.exit_zero, show_stats=args.stats)

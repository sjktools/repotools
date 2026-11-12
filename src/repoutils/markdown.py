"""Markdown reading the prose checks share: skipping code fences, and pulling
the prose sentences out of a document.

It reads Markdown the way a README uses it, by line and by regular expression,
not as a full CommonMark parser.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

# A code span: a run of backticks, then the shortest text up to a run of the same length; an unmatched run is literal.
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# What a code span becomes in a sentence's text: one word, whatever the code was.
CODE = "CODE"

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_LIST_ITEM_RE = re.compile(r"(?:[-*+]|\d+[.)])\s+(.*)")
_RULE_RE = re.compile(r"[-*_=\s]{3,}")
_HTML_RE = re.compile(r"</?[a-zA-Z]")
_LINK_DEFINITION_RE = re.compile(r"\[[^\]]+\]:\s")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])")
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_)(\S.*?\S|\S)\1")
# "etc." isn't here: it usually ends its sentence, and it only splits one before a capital anyway.
_ABBREVIATION_RE = re.compile(r"\b(?:e\.g|i\.e|vs)\.", re.IGNORECASE)
# The gap between two sentences: after closing punctuation, before a capital, a digit or code.
_BOUNDARY_RE = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][\"')\]]))\s+(?=[\"'(\[]?[A-Z0-9])")
_SENTENCE_END_RE = re.compile(r"[.!?:][\"')\]]?$")
# A line that ends a sentence, so that the next line starts one, even with a lowercase word.
_LINE_END_RE = re.compile(r"[.!?][\"')\]]?$")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’.-]*")
# A sentence has at least this many words; anything shorter is a label or a fragment.
MIN_WORDS = 3


@dataclass(frozen=True)
class Sentence:
    """A prose sentence, with the number of the line it starts on, and whether it's in a list item."""

    line: int
    text: str
    in_list: bool = False


def fence_marker(line: str) -> tuple[str, int] | None:
    """The fence character and its run length, if the line opens or closes a code fence."""
    stripped = line.lstrip()
    if stripped.startswith("```"):
        return ("`", len(stripped) - len(stripped.lstrip("`")))
    if stripped.startswith("~~~"):
        return ("~", len(stripped) - len(stripped.lstrip("~")))
    return None


def unfenced_lines(text: str) -> Iterator[tuple[int, str]]:
    """Each line outside a code fence, with its number (from 1); the fence lines themselves are left out."""
    fence: tuple[str, int] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = fence_marker(line)
        if marker is not None:
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and marker[1] >= fence[1] and not line.strip().strip(fence[0]):
                fence = None
            continue
        if fence is None:
            yield number, line


def words(text: str) -> list[str]:
    """The words of a sentence's text; a code span counts as one."""
    return _WORD_RE.findall(text)


def _clean(line: str) -> str:
    line = INLINE_CODE_RE.sub(CODE, line)
    line = _IMAGE_RE.sub("", line)
    line = _LINK_RE.sub(r"\1", line)
    line = _TAG_RE.sub("", line)
    line = _URL_RE.sub("URL", line)
    line = _EMPHASIS_RE.sub(r"\2", line)
    return " ".join(line.split())


def _skipped(stripped: str) -> bool:
    # Headings, tables, block quotes (often terminal output), rules, HTML and link definitions aren't prose.
    return bool(
        stripped.startswith(("#", "|", ">"))
        or _RULE_RE.fullmatch(stripped)
        or _HTML_RE.match(stripped)
        or _LINK_DEFINITION_RE.match(stripped)
    )


def _blocks(text: str) -> list[tuple[bool, list[tuple[int, str]]]]:
    """The paragraphs and list items, each as whether it's in a list item and its (line number, cleaned text) lines."""
    # Keep a comment's newlines, so the lines after it keep their numbers.
    text = _COMMENT_RE.sub(lambda match: "\n" * match[0].count("\n"), text)
    blocks: list[tuple[bool, list[tuple[int, str]]]] = []
    block: list[tuple[int, str]] = []
    block_in_list = False
    after_blank = True
    in_list = False

    def flush() -> None:
        nonlocal block
        if block:
            blocks.append((block_in_list, block))
        block = []

    for number, line in unfenced_lines(text):
        stripped = line.strip()
        if not stripped:
            flush()
            after_blank = True
            continue
        indented = line.startswith(("    ", "\t"))
        # Indented text after a blank line is a code block, unless it continues a list item.
        if _skipped(stripped) or (indented and after_blank and not in_list):
            flush()
            after_blank = indented
            continue
        after_blank = False
        item = _LIST_ITEM_RE.match(stripped)
        if item:
            flush()
            in_list = True
            stripped = item[1]
        elif not block and not line.startswith((" ", "\t")):
            # A paragraph that isn't indented at all ends the list; a "- " item's own paragraphs have two spaces.
            in_list = False
        cleaned = _clean(stripped)
        if cleaned:
            if not block:
                block_in_list = in_list
            block.append((number, cleaned))
    flush()
    return blocks


def _split(block: list[tuple[int, str]], in_list: bool) -> Iterator[Sentence]:
    starts: list[tuple[int, int]] = []  # (offset into the joined text, line number)
    offset = 0
    for number, line in block:
        starts.append((offset, number))
        offset += len(line) + 1
    joined = " ".join(line for _, line in block)
    # Hide an abbreviation's full stop from the boundary search, without moving anything.
    hidden = _ABBREVIATION_RE.sub(lambda match: match[0][:-1] + "\0", joined)
    # Each gap between two sentences, as where one ends and where the next begins.
    gaps = {(boundary.start(), boundary.end()) for boundary in _BOUNDARY_RE.finditer(hidden)}
    for (line_start, _), (next_start, _) in zip(starts, starts[1:], strict=False):
        if _LINE_END_RE.search(hidden[line_start : next_start - 1]):
            gaps.add((next_start - 1, next_start))
    begin = 0
    for end, next_begin in [*sorted(gaps), (len(joined), len(joined))]:
        first_line = max(number for start, number in starts if start <= begin)
        yield Sentence(first_line, joined[begin:end].strip(), in_list)
        begin = next_begin


def prose_sentences(text: str) -> list[Sentence]:
    """The document's prose sentences, in order.

    Code blocks, headings, tables, block quotes and HTML are left out. Inline
    code becomes the single word ``CODE``, a link becomes its text, and a bare
    URL becomes ``URL``. A paragraph's wrapped lines are joined, and each list
    item is read on its own, and its sentences are marked. A sentence ends
    before a capital, a digit or code, or at the end of a line. Text that isn't
    a sentence (no closing punctuation, or under three words) is dropped, such
    as a terse bullet.
    """
    return [
        sentence
        for in_list, block in _blocks(text)
        for sentence in _split(block, in_list)
        if _SENTENCE_END_RE.search(sentence.text) and len(words(sentence.text)) >= MIN_WORDS
    ]

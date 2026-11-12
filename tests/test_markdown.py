import textwrap

from repoutils.markdown import Sentence, prose_sentences, unfenced_lines, words


def _sentences(text: str) -> list[tuple[int, str]]:
    return [(sentence.line, sentence.text) for sentence in prose_sentences(textwrap.dedent(text))]


def test_unfenced_lines_skip_fences() -> None:
    text = textwrap.dedent("""\
        before
        ````markdown
        ```
        inside
        ```
        ````
        between
        ~~~
        ``` not a close
        ~~~
        after
        """)
    assert list(unfenced_lines(text)) == [(1, "before"), (7, "between"), (11, "after")]


def test_only_prose_is_read() -> None:
    assert _sentences("""\
        # A heading. With two sentences.

        The first paragraph is prose.

        | A table. | Is not prose. |
        | --- | --- |

        > A quote is not prose.

        ---

        <p>Neither is HTML. Of any kind.</p>
        <!-- A comment
        that runs over lines. -->
        The line after the comment is prose.

        [label]: https://example.com/ "A link definition."

        ```python
        print("Code is not prose.")
        ```

            An indented block is code.

        The last paragraph is prose.
        """) == [
        (3, "The first paragraph is prose."),
        (15, "The line after the comment is prose."),
        (25, "The last paragraph is prose."),
    ]


def test_inline_markup_is_reduced_to_its_words() -> None:
    assert _sentences("""\
        Run `uv run repoutils --help` and ``a ` b`` to see it.
        See [the guide](https://example.com/guide) or [the other one][other].
        It is at https://example.com/a.b?c=d and ![a logo](logo.png) nowhere else.
        This is **very** important, _really_ it is, <b>truly</b>.
        """) == [
        (1, "Run CODE and CODE to see it."),
        (2, "See the guide or the other one."),
        (3, "It is at URL and nowhere else."),
        (4, "This is very important, really it is, truly."),
    ]
    assert words("Run CODE and CODE to see it.") == ["Run", "CODE", "and", "CODE", "to", "see", "it."]


def test_wrapped_paragraphs_are_joined_and_split_into_sentences() -> None:
    assert _sentences("""\
        This sentence starts here
        and ends here. This one starts on the
        second line! Does a question count?
        "Quoted sentences do." (And bracketed ones.) `code` can start one too.
        """) == [
        (1, "This sentence starts here and ends here."),
        (2, "This one starts on the second line!"),
        (3, "Does a question count?"),
        (4, '"Quoted sentences do."'),
        (4, "(And bracketed ones.)"),
        (4, "CODE can start one too."),
    ]


def test_abbreviations_and_decimals_do_not_end_a_sentence() -> None:
    assert _sentences("""\
        Use a tool, e.g. This one, i.e. The best, etc. and so on vs. Others.
        It needs Python 3.12 or later. version 2 works too.
        """) == [
        (1, "Use a tool, e.g. This one, i.e. The best, etc. and so on vs. Others."),
        (2, "It needs Python 3.12 or later. version 2 works too."),
    ]


def test_etc_can_end_a_sentence() -> None:
    assert _sentences("It reads tabs, spaces, etc. Then it stops there.\n") == [
        (1, "It reads tabs, spaces, etc."),
        (1, "Then it stops there."),
    ]


def test_a_line_that_ends_a_sentence_starts_the_next_one() -> None:
    assert _sentences("""\
        The lock leaves Windows out.
        repoutils itself is pure Python. It needs a tool, e.g.
        this one, which is wrapped
        onto three lines.
        """) == [
        (1, "The lock leaves Windows out."),
        (2, "repoutils itself is pure Python."),
        (2, "It needs a tool, e.g. this one, which is wrapped onto three lines."),
    ]


def test_list_items_are_read_one_by_one_and_fragments_are_dropped() -> None:
    assert _sentences("""\
        Pros:

        - Portable, no dependencies
        - It is very fast. It is also small.
        * A wrapped item starts here
          and ends on the next line.

            A paragraph inside the item is prose.

        1. Numbered items count too.
        2) Too short.

        Back to a paragraph.

            Now this is code.
        """) == [
        (4, "It is very fast."),
        (4, "It is also small."),
        (5, "A wrapped item starts here and ends on the next line."),
        (8, "A paragraph inside the item is prose."),
        (10, "Numbered items count too."),
        (13, "Back to a paragraph."),
    ]


def test_sentences_in_list_items_are_marked() -> None:
    text = "Before the list.\n\n- An item is here.\n  It goes on here.\n\n  Its second paragraph.\n\nAfter the list.\n"
    assert [(sentence.text, sentence.in_list) for sentence in prose_sentences(text)] == [
        ("Before the list.", False),
        ("An item is here.", True),
        ("It goes on here.", True),
        ("Its second paragraph.", True),
        ("After the list.", False),
    ]


def test_a_line_of_markup_alone_is_nothing() -> None:
    assert prose_sentences("![logo](logo.png)\n\n") == []
    assert prose_sentences("") == []
    assert prose_sentences("One two three.") == [Sentence(1, "One two three.")]

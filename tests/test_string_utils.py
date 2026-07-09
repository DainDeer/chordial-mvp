"""chunk_message tests - including the regression for the latent NameError:
the over-long-paragraph branch called `self._split_into_sentences` (a method
that never existed; the helper is module-level `split_into_sentences`), so any
single paragraph longer than max_length crashed the send path. telegram's
4096-char chunking leans on this helper, so it's load-bearing now.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils.string_utils import chunk_message, split_into_sentences  # noqa: E402


def test_short_message_is_one_chunk():
    assert chunk_message("hi there") == ["hi there"]


def test_paragraphs_split_across_chunks():
    p1 = "a" * 1200
    p2 = "b" * 1200
    chunks = chunk_message(f"{p1}\n\n{p2}", max_length=2000)
    assert len(chunks) == 2
    assert chunks[0] == p1
    assert chunks[1] == p2


def test_single_overlong_paragraph_does_not_crash():
    """THE regression: one paragraph > max_length used to raise NameError
    (self._split_into_sentences on a module function)."""
    long_paragraph = " ".join(
        f"sentence number {i} keeps this paragraph rolling."
        for i in range(120)
    )
    assert len(long_paragraph) > 2000
    chunks = chunk_message(long_paragraph, max_length=2000)
    assert len(chunks) >= 2
    assert all(len(c) <= 2000 for c in chunks)
    # nothing was silently dropped (whitespace shifts aside)
    assert sum(len(c.replace(" ", "")) for c in chunks) == len(long_paragraph.replace(" ", ""))


def test_respects_custom_max_length():
    text = " ".join(f"word{i} and some filler here." for i in range(400))
    for limit in (2000, 4096):
        chunks = chunk_message(text, max_length=limit)
        assert all(len(c) <= limit for c in chunks)


def test_split_into_sentences_basic():
    assert split_into_sentences("one. two! three?") == ["one.", "two!", "three?"]

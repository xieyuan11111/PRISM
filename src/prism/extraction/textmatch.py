"""Safe quote-to-source matching used only to locate evidence spans.

Locating a model-proposed quote may collapse whitespace runs to a single
space and map a closed set of single-character Unicode punctuation
lookalikes to their ASCII equivalents, because models routinely collapse
double spaces, swap NBSP for spaces, or straighten curly quotes while
copying.  Whitespace is never deleted — distinct words can never fuse —
and no general Unicode normalization is applied, so lookalike classes such
as circled digits or full-width letters never fold to their ASCII
lookalikes.  Every table entry maps one character to one character, so a
folded match can never straddle a character boundary that does not exist
in the original text.  The folding is applied symmetrically to the quote
and the material, is never case-insensitive and never semantic, so a
paraphrase cannot match.  Spans returned to callers are always continuous,
character-exact slices of the original material text, and each span is
re-verified by folding the slice before it is returned.
"""

from __future__ import annotations

import re

# Closed, symmetric lookalike folds.  Every entry maps exactly one
# character to exactly one ASCII character; nothing else is folded.  In
# particular the ellipsis is deliberately absent: mapping it to "..." is a
# multi-character expansion that would let a "." quote match an ellipsis.
_PUNCTUATION_FOLDS = str.maketrans(
    {
        "\u2018": "'",  # ‘ left single quotation mark
        "\u2019": "'",  # ’ right single quotation mark
        "\u201b": "'",  # ‛ single high-reversed-9 quotation mark
        "\u201c": '"',  # “ left double quotation mark
        "\u201d": '"',  # ” right double quotation mark
        "\u2032": "'",  # ′ prime
        "\u2033": '"',  # ″ double prime
        "\u2010": "-",  # ‐ hyphen
        "\u2011": "-",  # ‑ non-breaking hyphen
        "\u2012": "-",  # ‒ figure dash
        "\u2013": "-",  # – en dash
        "\u2014": "-",  # — em dash
        "\u2212": "-",  # − minus sign
    }
)

_WHITESPACE_RUN = re.compile(r"\s+")


def fold_for_location(text: str) -> str:
    """Whitespace-run- and lookalike-punctuation-insensitive form for locating.

    Every maximal whitespace run folds to a single space and boundary
    whitespace is trimmed, so the fold keeps word boundaries: ``"not able"``
    never folds like ``"notable"``.  Non-whitespace characters change only
    through the closed single-character table above; no general Unicode
    normalization is applied, so ``①``/``Ｃ``/``…`` stay distinct from
    ``1``/``C``/``...``.
    """

    return " ".join(
        part
        for part in _WHITESPACE_RUN.split(text.translate(_PUNCTUATION_FOLDS))
        if part
    )


def _folded_units(content: str) -> tuple[tuple[str, int, int], ...]:
    """Folded stream units of the content with their original spans.

    A unit is either one non-whitespace character or one maximal whitespace
    run (which folds to a single space), so every unit contributes exactly
    one folded character and a folded match can only start and end at unit
    edges, never inside a would-be multi-character expansion.
    """

    units: list[tuple[str, int, int]] = []
    at = 0
    for run in _WHITESPACE_RUN.finditer(content):
        for char in content[at : run.start()]:
            units.append((char.translate(_PUNCTUATION_FOLDS), at, at + 1))
            at += 1
        units.append((" ", run.start(), run.end()))
        at = run.end()
    for char in content[at:]:
        units.append((char.translate(_PUNCTUATION_FOLDS), at, at + 1))
        at += 1
    return tuple(units)


def resolve_verbatim_spans(content: str, quote: str) -> tuple[tuple[int, int], ...]:
    """All continuous content slices whose folded form equals the folded quote.

    Each span is (start, end) over whole original characters with end
    exclusive.  Matches are found over the unit stream above, so a span
    always begins and ends on non-whitespace characters of the original
    text, and each candidate span is re-verified by folding the slice
    before it is returned.
    """

    folded_quote = fold_for_location(quote)
    if not folded_quote:
        return ()
    units = _folded_units(content)
    haystack = "".join(unit[0] for unit in units)
    spans: list[tuple[int, int]] = []
    at = haystack.find(folded_quote)
    while at >= 0:
        start, end = units[at][1], units[at + len(folded_quote) - 1][2]
        if fold_for_location(content[start:end]) == folded_quote:
            spans.append((start, end))
        at = haystack.find(folded_quote, at + 1)
    return tuple(spans)


def paragraph_spans(content: str) -> tuple[tuple[int, int, int], ...]:
    """One-based spans of non-empty lines, matching PRISM paragraph numbering.

    PRISM counts every non-empty line as one paragraph (blank lines are
    skipped), the same contract ``EvidenceStore.locate`` applies.  Each item
    is (paragraph number, start, end) with end exclusive, covering the
    stripped text of the line.
    """

    spans: list[tuple[int, int, int]] = []
    number = 0
    offset = 0
    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if body.strip():
            number += 1
            spans.append(
                (
                    number,
                    offset + (len(body) - len(body.lstrip())),
                    offset + len(body.rstrip()),
                )
            )
        offset += len(line)
    return tuple(spans)

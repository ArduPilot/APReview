#!/usr/bin/env python3
"""Read the verdict out of an APReview comment.

Its own module because it is the one part of the project sync with no network
in it, and the part most likely to be wrong: the verdict is prose written by a
model, and prose varies.

Two sources, in order:

  1. A marker the review itself emits, which is exact:
         <!-- apreview: verdict=ACCEPT head=5574a60eb2 -->
     It renders as nothing on GitHub. Reviews written since it was added carry
     one; nothing else has to be guessed for those.

  2. Failing that, the shapes found in the comments already posted. A survey of
     the 168 open labelled PRs on 2026-09-26 turned up seven, which is the
     reason this is not a one-line regex:

         **Verdict: COMMENT - no blockers.**     Verdict: **COMMENT** - ...
         Verdict: COMMENT                        **REQUEST CHANGES** - ...
         **APPROVE - no blockers.**              ## COMMENT - one real gap
         **... Verdict stays APPROVE.**

     The anchors are ordered by how strongly each means "this is the verdict"
     rather than a word used in passing. Every one of them requires a marker -
     a "Verdict" label, a heading, or the start of a bold run - because the
     bare words appear constantly in ordinary review prose: 10 of the 168 name
     a second verdict within the first 400 characters of the right one.

The fallback reaches 94% of the existing backlog. The rest either state no
verdict (a draft, reviewed as guidance) or are followup notes that deliberately
carry none, and they are reported as unknown rather than guessed at.
"""
import re

ACCEPT, COMMENT, REQUEST = "ACCEPT", "COMMENT", "REQUEST CHANGES"
VERDICTS = (ACCEPT, COMMENT, REQUEST)

# APPROVE and ACCEPT are the same verdict under two names; GitHub's own review
# state is APPROVE, and the project column says ACCEPT.
_WORDS = r"(ACCEPT|APPROVE|COMMENT|REQUEST\s+CHANGES)"

MARKER = re.compile(r"<!--\s*apreview:\s*([^>]*?)-->", re.I)
_FIELD = re.compile(r"(\w+)\s*=\s*([^\s]+)")

# A comment we superseded. Its verdict is not the current one.
DEPRECATED = "> **Deprecated"

# Ordered. Each requires something that means "this is the verdict"; none of
# them will match a verdict word used in passing.
_ANCHORS = (
    # "the verdict moves from REQUEST CHANGES to COMMENT" - the one it moved TO.
    # This has to be tried before anything else: a re-review states the old
    # verdict and the new one in that order, and every other anchor would take
    # the old one. Four of the 168 read backwards before this existed.
    ("verdict-moves", re.compile(
        r"verdict\b[^.\n]{0,40}?\b(?:moves?|moved|changes?|changed|goes|went|drops?|rises?)\b"
        r"[^.\n]{0,40}?\bto\s+\*{0,2}\s*" + _WORDS, re.I)),
    # "Verdict stays APPROVE", "Verdict now APPROVE", "Verdict remains COMMENT"
    ("verdict-stays", re.compile(
        r"verdict\b\s*(?:\*{0,2}\s*)?(?:stays|remains|is|now|unchanged\s+at)\s+\*{0,2}\s*"
        + _WORDS, re.I)),
    # "Verdict: X", "**Verdict: X", "Verdict: **X"
    ("verdict-label", re.compile(r"\*{0,2}\s*Verdict:\s*\*{0,2}\s*" + _WORDS, re.I)),
    # "## COMMENT - one real gap"
    ("heading", re.compile(r"^#{1,4}\s*\**\s*" + _WORDS + r"\b", re.I | re.M)),
    # "**REQUEST CHANGES** - ..." and "**APPROVE - no blockers.**"
    ("bold-opener", re.compile(r"\*\*\s*" + _WORDS + r"\b", re.I)),
)
# Nothing here matches a bare list. "the three passes disagreed on the overall
# verdict - APPROVE, COMMENT and REQUEST CHANGES" needs no special handling
# because none of the anchors fires on it: there is no colon, no "stays", no
# heading and no bold run. That sentence is why these are anchored rather than
# a search for the words.


def normalise(word):
    """One spelling per verdict, so the project column has three values."""
    w = re.sub(r"\s+", " ", (word or "").strip().upper())
    if w in ("ACCEPT", "APPROVE", "APPROVED"):
        return ACCEPT
    if w in ("REQUEST CHANGES", "REQUEST_CHANGES", "REQUESTCHANGES"):
        return REQUEST
    return COMMENT if w == "COMMENT" else None


def from_marker(body):
    """The verdict the review stated outright, or None."""
    m = MARKER.search(body or "")
    if not m:
        return None
    fields = dict(_FIELD.findall(m.group(1)))
    return normalise(fields.get("verdict", "").replace("_", " "))


def from_prose(body):
    """(verdict, which anchor matched), or (None, None)."""
    body = body or ""
    for name, pat in _ANCHORS:
        m = pat.search(body)
        if m:
            v = normalise(m.group(m.lastindex))
            if v:
                return v, name
    return None, None


def of_comment(body):
    """This one comment's verdict and where it came from."""
    v = from_marker(body)
    if v:
        return v, "marker"
    return from_prose(body)


def is_deprecated(body):
    return (body or "").lstrip().startswith(DEPRECATED)


def of_thread(bodies):
    """The verdict of a PR, from our comments oldest-first.

    The newest comment that states one wins. That is not the same as the newest
    comment: a followup note says the author's code moved and carries no
    verdict, and reading it as "no review" would drop a reviewed PR off the
    board. Superseded comments are skipped entirely - their verdict is stale,
    and it would otherwise outrank a followup that has none.
    """
    for body in reversed([b for b in bodies if not is_deprecated(b)]):
        v, how = of_comment(body)
        if v:
            return v, how
    return None, None

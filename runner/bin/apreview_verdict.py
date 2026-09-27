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

# Where a verdict word is allowed to end. Two strengths, because the anchors
# are not equally specific - both cases below came from an adversarial review
# on 2026-09-27.
#
# An anchor carrying the word "Verdict" has already established what it is
# talking about, so the verdict may be followed by more sentence. It still may
# not be the start of a longer word: "Verdict: acceptable once the crash is
# fixed" read as ACCEPT before this.
_END_WORD = r"\b(?!\w)"
# A heading or a bold run is a verdict only if that is all it is. Otherwise
# "## Comment on test coverage" is a COMMENT verdict, and so is
# "Please **comment** on the test plan".
_END_ALONE = _END_WORD + r"(?=\s*(?:\*{0,2})\s*(?:[-–—:.,;!?)\]]|$))"

# Words that make the sentence hypothetical or deny it. A verdict inside one is
# not a verdict: "the verdict does not move to ACCEPT" and "if tests pass, the
# verdict moves to ACCEPT" both said ACCEPT before this existed.
_UNREAL = re.compile(
    r"\b(?:if|once|unless|until|should|would|could|when|provided|assuming|"
    r"not|n't|never|cannot|rather\s+than|instead\s+of|was|were|previously|"
    r"had\s+been|no\s+longer)\b", re.I)


def _unreal_before(text, start, window=60):
    """Is this match inside a negated or hypothetical clause?

    Only the current clause is examined - a full stop, semicolon, newline or
    bold run ends it - because "No blockers. Verdict: ACCEPT" is a perfectly
    ordinary thing for a review to say.
    """
    clause = text[max(0, start - window):start]
    clause = re.split(r"[.;\n]|\*\*", clause)[-1]
    return bool(_UNREAL.search(clause))


MARKER = re.compile(r"<!--\s*apreview:\s*([^>]*?)-->", re.I)
_FIELD = re.compile(r"(\w+)\s*=\s*([^\s]+)")

# A comment we superseded. Its verdict is not the current one.
DEPRECATED = "> **Deprecated"

# Quoted text and fenced code are somebody else's words, or our own repeated
# back at us. A maintainer disagreeing with "> **Verdict: ACCEPT**" was read as
# a fresh ACCEPT.
_QUOTED = re.compile(r"^\s{0,3}>.*$", re.M)
_FENCED = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INDENTED_CODE = re.compile(r"^(?: {4}|\t).*$", re.M)


def strip_quotes(body):
    """The comment's own prose, with quotations and code removed."""
    body = _FENCED.sub("\n", body or "")
    body = _QUOTED.sub("", body)
    return _INDENTED_CODE.sub("", body)


# Ordered. Each requires something that means "this is the verdict"; none will
# match a verdict word used in passing.
_ANCHORS = (
    # "the verdict moves from REQUEST CHANGES to COMMENT", "verdict was X, now
    # Y", "downgraded to X" - always the one it ended at, never the one it left.
    # Tried first: a re-review states the old verdict and the new one in that
    # order, and every other anchor would take the old one.
    ("verdict-moves", re.compile(
        r"verdict\b[^.\n]{0,60}?(?:"
        r"\b(?:moves?|moved|changes?|changed|goes|went|drops?|dropped|rises?|"
        r"rose|downgraded|upgraded|lowered|raised)\b[^.\n]{0,40}?\bto\s+"
        r"|\bis\s+now\s+|\bnow\s+"
        r")\*{0,2}\s*" + _WORDS + _END_WORD, re.I)),
    # "Verdict stays APPROVE", "Verdict remains COMMENT"
    ("verdict-stays", re.compile(
        r"verdict\b\s*(?:\*{0,2}\s*)?(?:stays|remains|unchanged\s+at)\s+\*{0,2}\s*"
        + _WORDS + _END_WORD, re.I)),
    # "Verdict: X", "**Verdict: X", "Verdict: **X"
    ("verdict-label", re.compile(
        r"\*{0,2}\s*Verdict\s*:\s*\*{0,2}\s*" + _WORDS + _END_WORD, re.I)),
    # "## COMMENT - one real gap". The whole heading has to be the verdict.
    ("heading", re.compile(
        r"^#{1,4}\s*\**\s*" + _WORDS + _END_ALONE, re.I | re.M)),
    # "**REQUEST CHANGES** - ..." and "**APPROVE - no blockers.**", at the start
    # of a line or a sentence. Mid-sentence bold is emphasis, not a verdict:
    # "Please **comment** on the test plan" is not a COMMENT verdict.
    ("bold-opener", re.compile(
        r"(?:^|(?<=[.!?])\s|(?<=[.!?]\n))\s*\*\*\s*" + _WORDS + _END_ALONE,
        re.I | re.M)),
)


def normalise(word):
    """One spelling per verdict, so the project column has three values."""
    w = re.sub(r"\s+", " ", (word or "").strip().upper())
    if w in ("ACCEPT", "APPROVE", "APPROVED"):
        return ACCEPT
    if w in ("REQUEST CHANGES", "REQUEST_CHANGES", "REQUESTCHANGES"):
        return REQUEST
    return COMMENT if w == "COMMENT" else None


def from_marker(body):
    """The verdict the review stated outright, or None.

    Read from the comment's own text: a marker inside a quotation is somebody
    showing what we said, not us saying it.
    """
    m = MARKER.search(strip_quotes(body))
    if not m:
        return None
    fields = dict(_FIELD.findall(m.group(1)))
    return normalise(fields.get("verdict", "").replace("_", " "))


def from_prose(body):
    """(verdict, which anchor matched), or (None, None).

    Unknown is a safe answer and a wrong verdict is not, so anything ambiguous
    returns nothing rather than a guess: the board then keeps the last verdict
    that was stated plainly, or leaves the PR off.
    """
    body = strip_quotes(body)
    for name, pat in _ANCHORS:
        for m in pat.finditer(body):
            if _unreal_before(body, m.start(m.lastindex)):
                continue
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

#!/usr/bin/env python3
"""Read the verdict out of an APReview comment.

Every review comment states its verdict on its second line, directly under
the AI-generated marker line, so that a person skimming the PR sees the
conclusion before the reasoning and a program does not have to guess:

    **Automated review note — AI-generated (Claude), validated against ...
    **Verdict: REQUEST CHANGES**

That line is the verdict. Comments written before 2026-09-28 said it in prose
instead, in seven different phrasings, and two rounds of adversarial review
each found ways to read the wrong one out of them; the prose parser now lives
only in verdict-backfill.py, which rewrote the existing comments to carry the
line. An older comment may still carry the invisible marker it used briefly,
    <!-- apreview: verdict=ACCEPT head=5574a60eb2 -->
which is read as a fallback. Anything else is unknown, and unknown is a safe
answer: the board keeps the last verdict that was stated, or leaves the PR off.
"""
import re

ACCEPT, COMMENT, REQUEST = "ACCEPT", "COMMENT", "REQUEST CHANGES"
VERDICTS = (ACCEPT, COMMENT, REQUEST)

# APPROVE and ACCEPT are the same verdict under two names; GitHub's own review
# state is APPROVE, and the project column says ACCEPT.
_WORDS = r"(ACCEPT|APPROVE|COMMENT|REQUEST[\s_]+CHANGES)"

# The whole line, allowing bold around either part and a trailing full stop.
VERDICT_LINE = re.compile(
    r"^\s*\**\s*Verdict\s*:\s*\**\s*" + _WORDS + r"[\s*.]*$", re.I)
# How many lines from the top the verdict line may sit. Line 1 is the marker
# line; the verdict is line 2. A little slack for a blank line or an old
# invisible marker in between, and no more: a "Verdict:" deep in the body is
# discussion, not the declaration.
HEAD_LINES = 5

MARKER = re.compile(r"^<!--\s*apreview:\s*([^>]*?)-->", re.I | re.M)
_FIELD = re.compile(r"(\w+)\s*=\s*([^\s]+)")

# A comment we superseded. Its verdict is not the current one.
DEPRECATED = "> **Deprecated"


def normalise(word):
    """One spelling per verdict, so the project column has three values."""
    w = re.sub(r"[\s_]+", " ", (word or "").strip().upper())
    if w in ("ACCEPT", "APPROVE", "APPROVED"):
        return ACCEPT
    if w == "REQUEST CHANGES":
        return REQUEST
    return COMMENT if w == "COMMENT" else None


def from_line(body):
    """The verdict stated on its own line near the top, or None."""
    for line in (body or "").split("\n")[:HEAD_LINES]:
        m = VERDICT_LINE.match(line)
        if m:
            return normalise(m.group(1))
    return None


def from_marker(body):
    """The verdict from the invisible marker some 2026-09 comments carry."""
    m = MARKER.search(body or "")
    if not m:
        return None
    fields = dict(_FIELD.findall(m.group(1)))
    return normalise(fields.get("verdict", ""))


def of_comment(body):
    """This one comment's verdict and where it came from."""
    v = from_line(body)
    if v:
        return v, "line"
    v = from_marker(body)
    if v:
        return v, "marker"
    return None, None


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

#!/usr/bin/env python3
"""Put the verdict line on the review comments written before it existed.

Every review comment now states its verdict on its second line - see
apreview_verdict.py. The comments posted before 2026-09-28 said it in prose,
and this is the one place the prose parser survives: it reads each PR's newest
live AI comment, works out what it concluded, and inserts

    **Verdict: REQUEST CHANGES**

directly under the AI-generated marker line. Only the newest live comment on
each PR is touched; superseded ones stay as they were.

    verdict-backfill.py                 the table: what each comment would get
    verdict-backfill.py --apply         edit the comments
    verdict-backfill.py --ambiguous     show the ones it cannot read, in full
    verdict-backfill.py --set ArduPilot/ardupilot#123=COMMENT ...
                                        decide one by hand (a human read it)
    verdict-backfill.py --only ArduPilot/ardupilot#123 ...

Two authors wrote these comments, so two identities edit them: comments by the
bot go out under GH_TOKEN (the bot's token, as review-env.sh exports it), and
comments by a person under the box's own gh login with that token unset. Who
each identity is gets checked before anything is written, and a comment whose
author is neither is left alone and reported.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import apreview_verdict as V                                       # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# the search, the gh transport and the account rules are the board's
PS = _load("project_sync", "project-sync.py")

normalise = V.normalise

# --- the prose parser, retired from apreview_verdict.py on 2026-09-28 ----------
# Its anchors were built from the seven phrasings found on the 168 open
# labelled PRs surveyed on 2026-09-26. It refuses a comment that declares two
# different verdicts rather than choosing between them.

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

# Words that make the sentence hypothetical, deny it, or put it in the past. A
# verdict inside one is not a verdict: "the verdict does not move to ACCEPT",
# "if tests pass, the verdict moves to ACCEPT" and "Previous verdict: ACCEPT"
# all said ACCEPT before this existed. Behind the verdict, only the words that
# hedge it count: "Verdict: COMMENT - all four previous BUGs are fixed" is a
# plain COMMENT.
_HEDGE = (r"if|once|unless|until|should|would|could|when|provided|assuming|"
          r"otherwise|else|only")
_DENIED = r"not|never|cannot|rather\s+than|instead\s+of"
_HISTORY = (r"was|were|previous(?:ly)?|prior|earlier|original(?:ly)?|"
            r"initial(?:ly)?|former(?:ly)?|old|last|had\s+been|no\s+longer")


def _words(*groups):
    # n't has no word boundary in front of it: "doesn't" is one word
    return re.compile(r"(?:\b(?:%s)|n't)\b" % "|".join(groups), re.I)


_UNREAL_BEFORE = _words(_HEDGE, _DENIED, _HISTORY)
# "verdict moves from X to Y" is about history by nature - "was X, now Y",
# "all blockers from my previous comment are fixed, so the verdict moves to
# Y" - so for that anchor only the hedges and denials count.
_UNREAL_BEFORE_MOVES = _words(_HEDGE, _DENIED)
_UNREAL_AFTER = _words(_HEDGE)


def _unreal(text, start, end, history=True, window=80):
    """Is this match inside a negated, hypothetical or historical clause?

    Only the current clause is examined - a full stop, semicolon or newline ends
    it - because "No blockers. Verdict: ACCEPT" is a perfectly ordinary thing
    for a review to say. Behind the match, a bold run or a dash ends it as
    well: what follows there is the explanation, not the condition.
    """
    before = re.split(r"[.;\n]", text[max(0, start - window):start])[-1]
    if (_UNREAL_BEFORE if history else _UNREAL_BEFORE_MOVES).search(before):
        return True
    after = re.split(r"[.;\n]|\*\*|\s[-\u2013\u2014]\s", text[end:end + window])[0]
    return bool(_UNREAL_AFTER.search(after))


# Quoted text and code are somebody else's words, or our own repeated back at
# us. A maintainer disagreeing with "> **Verdict: ACCEPT**" was read as a fresh
# ACCEPT, and so was an example marker inside backticks. Done by line, the way
# CommonMark reads it: a fence closes only at a fence of its own kind at least
# as long, or never; a quotation runs on into the plain lines under it until a
# blank line or a new block; a code span is any run of backticks matched by an
# equal run.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_QUOTE = re.compile(r"^ {0,3}>")
_BLOCK_START = re.compile(r"^ {0,3}(?:#{1,6}(?:\s|$)|[-*+]\s|\d{1,9}[.)]\s|"
                          r"(?:[-*_]\s*){3,}$|<)")
_INDENTED_CODE = re.compile(r"^(?: {4}|\t)")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n\s*\n).)+?)(?<!`)\1(?!`)", re.S)


def strip_quotes(body):
    """The comment's own prose, with quotations and code removed."""
    out, fence, quoting = [], None, False
    for line in (body or "").split("\n"):
        if fence:
            m = _FENCE.match(line)
            if (m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
                    and not line[m.end():].strip()):
                fence = None
            out.append("")
            continue
        m = _FENCE.match(line)
        if m and not (m.group(1)[0] == "`" and "`" in line[m.end():]):
            fence, quoting = m.group(1), False
            out.append("")
            continue
        if _QUOTE.match(line):
            quoting = True
            out.append("")
            continue
        if quoting and line.strip() and not _BLOCK_START.match(line):
            out.append("")          # lazy continuation: still the quotation
            continue
        quoting = False
        if _INDENTED_CODE.match(line):
            out.append("")
            continue
        out.append(line)
    return _CODE_SPAN.sub(" ", "\n".join(out))


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


def declarations(body):
    """Every verdict the comment declares, as (verdict, anchor), in anchor order.

    Declared, not mentioned: each anchor needs a marker that means "this is the
    verdict", and a match inside a hedged, denied or historical clause does not
    count. What is left is what the comment is actually claiming.
    """
    body = strip_quotes(body)
    out = []
    for name, pat in _ANCHORS:
        for m in pat.finditer(body):
            if _unreal(body, m.start(m.lastindex), m.end(m.lastindex),
                       history=(name != "verdict-moves")):
                continue
            v = normalise(m.group(m.lastindex))
            if v:
                out.append((v, name))
    return out


def from_prose(body):
    """(verdict, which anchor matched), or (None, None).

    Unknown is a safe answer and a wrong verdict is not. A comment is read only
    when everything it declares agrees: two different verdicts in declaration
    positions - a history, a condition the hedge words missed, an example -
    are refused rather than resolved, because each rule for resolving them has
    turned out to have a counter-example that reads the wrong one.
    """
    found = declarations(body)
    if len({v for v, _ in found}) != 1:
        return None, None
    return found[0]


# --- the comments ------------------------------------------------------------

SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 25, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      number url repository { nameWithOwner }
      comments(last: 100) { nodes { databaseId author { login } body } }
    } }
  }
}"""


def open_prs(owners):
    label = "label:" + ",".join(PS.LABELS)
    out = []
    for owner in owners:
        after = None
        while True:
            v = {"q": "is:pr is:open org:%s %s" % (owner, label)}
            if after:
                v["after"] = after
            d = PS.gh(SEARCH, **v)["search"]
            out += [n for n in d["nodes"] if n]
            if not d["pageInfo"]["hasNextPage"]:
                break
            after = d["pageInfo"]["endCursor"]
    return out


def newest_live(pr, accounts):
    """The comment that carries this PR's verdict, or None."""
    ours = [c for c in pr["comments"]["nodes"]
            if c and (c.get("author") or {}).get("login") in accounts
            and PS.AI_MARKER in (c.get("body") or "")]
    live = [c for c in ours if not V.is_deprecated(c["body"])]
    return live[-1] if live else None


def with_verdict_line(body, verdict):
    """The body with the verdict stated on line 2."""
    lines = body.split("\n")
    return "\n".join([lines[0], "**Verdict: %s**" % verdict] + lines[1:])


def decide(body, override=None):
    """(verdict, how) for a comment: the override if a human gave one, else
    the marker, else the prose, else nothing."""
    if V.from_line(body):
        return V.from_line(body), "already"
    if override:
        return override, "by hand"
    v = V.from_marker(body)
    if v:
        return v, "marker"
    return from_prose(body)


# --- identities ----------------------------------------------------------------

def _env(bot):
    env = dict(os.environ)
    if not bot:
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
    return env


def login(bot):
    p = subprocess.run(["gh", "api", "user", "--jq", ".login"], env=_env(bot),
                       capture_output=True, text=True, timeout=60)
    return p.stdout.strip() if p.returncode == 0 else None


def edit(repo, cid, body, bot):
    p = subprocess.run(["gh", "api", "-X", "PATCH",
                        "repos/%s/issues/comments/%s" % (repo, cid), "--input", "-"],
                       input=json.dumps({"body": body}), env=_env(bot),
                       capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        print("    gh: %s" % (p.stderr.strip().splitlines() or ["?"])[0])
    return p.returncode == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="edit the comments")
    ap.add_argument("--ambiguous", action="store_true",
                    help="print the comments no verdict can be read from")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VERDICT",
                    help="the verdict for one PR, decided by a person")
    ap.add_argument("--only", nargs="*", default=None, metavar="KEY")
    a = ap.parse_args(argv)

    overrides = {}
    for s in a.set:
        key, _, word = s.partition("=")
        v = normalise(word)
        if not v:
            sys.exit("not a verdict: %r" % word)
        overrides[key] = v

    accounts = tuple((os.environ.get("REVIEW_COMMENT_ACCOUNTS")
                      or "AP-Review tridge").split())
    who = {True: login(True) if os.environ.get("GH_TOKEN") else None,
           False: login(False)}
    print("edits as: bot=%s  box=%s" % (who[True], who[False]))

    prs = open_prs(PS.swept_owners())
    rows, done, failed = [], 0, 0
    for pr in sorted(prs, key=lambda p: (p["repository"]["nameWithOwner"], p["number"])):
        key = "%s#%d" % (pr["repository"]["nameWithOwner"], pr["number"])
        if a.only is not None and key not in a.only:
            continue
        c = newest_live(pr, accounts)
        if c is None:
            rows.append((key, "-", "no live comment", ""))
            continue
        verdict, how = decide(c["body"], overrides.get(key))
        author = (c.get("author") or {}).get("login")
        if how == "already":
            rows.append((key, author, "has line", verdict))
            continue
        if not verdict:
            rows.append((key, author, "AMBIGUOUS", ""))
            if a.ambiguous:
                print("\n===== %s  %s  by %s\n%s\n" % (key, pr["url"], author,
                                                        c["body"][:3000]))
            continue
        rows.append((key, author, how, verdict))
        if not a.apply:
            continue
        bot = author == who[True]
        if not bot and author != who[False]:
            print("  ! %s: written by %s, which neither identity can edit" % (key, author))
            failed += 1
            continue
        if edit(pr["repository"]["nameWithOwner"], c["databaseId"],
                with_verdict_line(c["body"], verdict), bot):
            done += 1
        else:
            print("  ! %s: edit failed" % key)
            failed += 1

    if not a.ambiguous:
        for key, author, how, verdict in rows:
            print("  %-44s %-10s %-12s %s" % (key, author, how, verdict))
    tally = {}
    for _, _, how, _ in rows:
        tally[how] = tally.get(how, 0) + 1
    print("\n%d PRs: %s" % (len(rows), ", ".join("%s %d" % kv for kv in sorted(tally.items()))))
    if a.apply:
        print("edited %d, failed %d" % (done, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PS.GhError as e:
        print("FATAL: %s" % e, file=sys.stderr)
        sys.exit(2)

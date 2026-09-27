#!/usr/bin/env python3
"""Keep the "APReview Results" project showing every open PR this system reviewed.

Standalone: it asks GitHub what is labelled and what we said, and makes the
project match. Nothing is passed in from a run, so the same command is correct
at the end of a review and from cron fifteen minutes later.

    project-sync.py                 make the project match reality
    project-sync.py --dry-run       say what it would change, change nothing
    project-sync.py --prune-only    only drop what is no longer eligible
    project-sync.py --show          print the table it would write

What belongs on the board: an open PR, in one of the swept repositories,
that we have posted a verdict on. A trigger label is what brings a PR to the
board - the label search is how new reviews are found - but losing the label
does not take it off: DevCallEU and DevCallTopic come off after the call, and
the PR stays until it is merged or closed. So the candidates are the labelled
PRs plus everything already on the board, each board row is refreshed from
the PR's own comments while it is open, and only a PR confirmed closed is
removed.

The verdict comes from the comment, not from the published report: each label's
report page is overwritten by the next run of that label, so it covers only the
most recent sweep - 35 of 168 open labelled PRs when this was written, against
166 for the comments. See apreview_verdict.py for how it is read.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import apreview_verdict as V                                       # noqa: E402
import repos as REPOS                                              # noqa: E402

TITLE = "APReview Results"
# Every comment this system posts carries it. Author alone is not enough:
# commenting moved from a person's account to AP-Review, so the older reviews
# are under a human who also writes ordinary comments - and an ordinary comment
# saying "I'd request changes here" must not be read as a verdict.
AI_MARKER = "AI-generated"
FIELD = "Result"
# GitHub has no built-in author field - Assignees and Reviewers are neither -
# so the board carries its own, filled in from the PR. Not called "Author":
# that name is reserved, and createProjectV2Field refuses it with "Name cannot
# have a reserved value".
AUTHOR = "PR Author"
LABELS = ("AIReview", "DevCallTopic", "DevCallEU")
# The option colours GitHub accepts, chosen so the board reads at a glance.
OPTIONS = [("ACCEPT", "GREEN", "Reviewed, no blockers"),
           ("COMMENT", "YELLOW", "Reviewed, notes but nothing blocking"),
           ("REQUEST CHANGES", "RED", "Reviewed, blocking findings")]


GH_TIMEOUT = 120               # no gh call may hang a run's exit path


def _run(args, stdin=None):
    """gh, with a deadline. The sync runs from a run's EXIT trap, and a request
    that never returns would hold the run lock open behind it."""
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              input=stdin, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise GhError("gh timed out after %ds" % GH_TIMEOUT)


class GhError(Exception):
    """A GitHub call failed.

    Never swallowed: an empty answer from a failed query is indistinguishable
    from "nothing is labelled", and acting on it would empty the board.
    """


def gh(query, **variables):
    """A GraphQL call. Numbers go as numbers.

    -f sends every value as a String, so an Int! argument is rejected with
    "could not coerce value". That failure is caught and read as "cannot tell",
    which is safe but useless: it made every deletion check answer unknown, so
    nothing was ever removed. -F sends a typed value.
    """
    args = ["gh", "api", "graphql", "-f", "query=" + query]
    for k, v in variables.items():
        typed = isinstance(v, (int, float)) and not isinstance(v, bool)
        args += ["-F" if typed else "-f", "%s=%s" % (k, v)]
    p = _run(args)
    if p.returncode != 0:
        # gh exits non-zero on a GraphQL error as well as on a transport
        # failure, and prints the message itself, so this is the path a scope
        # problem actually takes - not the errors list below.
        raise GhError(_explain((p.stderr or p.stdout).strip()))
    try:
        d = json.loads(p.stdout)
    except ValueError:
        raise GhError("unparseable reply: %s" % p.stdout[:200])
    if d.get("errors"):
        raise GhError(_explain("; ".join(e.get("message", "?")
                                         for e in d["errors"])))
    return d["data"]


SCOPE_HELP = ("\n       The box login needs the project scope. On the box:\n"
              "           gh auth refresh -s project,read:project\n"
              "       project-sync.sh deliberately does not use the AP-Review "
              "token, which is scoped to public_repo alone.")


def _explain(text):
    """A scope failure says what to do about it, not just what went wrong."""
    if "not been granted the required scopes" in text or "read:project" in text:
        return text[:300] + SCOPE_HELP
    return text[:400]


def gh_list(query, strings, lists):
    """gh() for a mutation that takes a list variable.

    -f sends everything as a string, so a [ID!]! has to go through --input as
    JSON rather than as repeated -f arguments.
    """
    body = {"query": query, "variables": dict(strings, **lists)}
    p = _run(["gh", "api", "graphql", "--input", "-"], stdin=json.dumps(body))
    if p.returncode != 0:
        raise GhError(_explain((p.stderr or p.stdout).strip()))
    d = json.loads(p.stdout)
    if d.get("errors"):
        raise GhError(_explain("; ".join(e.get("message", "?")
                                         for e in d["errors"])))
    return d["data"]


# --- what should be on the board ---------------------------------------------

def swept_owners(path=None):
    """The orgs we review, in the order repos.json first names them.

    Through repos.py rather than a path of its own: ~/review/bin is a symlink
    into the checkout, so a path built from __file__ without realpath looks for
    repos.json in ~/review and finds nothing. repos.py already gets that right,
    and one copy of the rule is the point.
    """
    if path:
        with open(path) as fh:
            d = json.load(fh)
    else:
        d = REPOS.load()
    repos = d if isinstance(d, list) else (d.get("repos") or [])
    names = [r if isinstance(r, str) else (r.get("repo") or "") for r in repos]
    seen, out = set(), []
    for n in names:
        owner = n.split("/")[0]
        if owner and owner not in seen:
            seen.add(owner)
            out.append(owner)
    return out


def our_comments(nodes, accounts):
    """The bodies of comments this system posted, oldest first.

    Both tests matter. The author must be one of ours, and the body must carry
    the AI-generated marker: commenting moved from a person's account to the
    bot, so the older reviews are authored by someone who also writes ordinary
    comments, and "I would request changes here" is not a verdict.
    """
    return [c["body"] for c in nodes
            if c and (c.get("author") or {}).get("login") in accounts
            and AI_MARKER in (c.get("body") or "")]


SEARCH = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 25, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      id number title url state isDraft
      author { login }
      repository { nameWithOwner }
      comments(last: 100) {
        pageInfo { hasPreviousPage startCursor }
        nodes { author { login } body }
      }
    } }
  }
}"""

# Older comments on one PR, walked backwards. comments(last: 100) gives the
# newest hundred, which is the window the verdict is almost always in; a PR
# busy enough to push it out of that window would otherwise lose its row
# entirely, because "no verdict" and "not reviewed" look the same here.
OLDER_COMMENTS = """
query($id: ID!, $before: String!) {
  node(id: $id) { ... on PullRequest {
    comments(last: 100, before: $before) {
      pageInfo { hasPreviousPage startCursor }
      nodes { author { login } body }
    }
  } }
}"""


# 500 comments back; beyond that, give up openly rather than walk a PR's whole
# history from a run's exit path. A cursor seen twice means the walk is not
# advancing, and is stopped for the same reason.
MAX_COMMENT_PAGES = 5


def verdict_of(pr, accounts, fetch_older=None):
    """The PR's verdict, paging back through comments until one is stated."""
    page = pr["comments"]
    seen = set()
    while True:
        verdict, how = V.of_thread(our_comments(page["nodes"], accounts))
        if verdict:
            return verdict, how
        info = page.get("pageInfo") or {}
        cursor = info.get("startCursor")
        if (not info.get("hasPreviousPage") or not cursor or cursor in seen
                or len(seen) >= MAX_COMMENT_PAGES):
            return None, None
        seen.add(cursor)
        if fetch_older is None:
            fetch_older = _older_comments
        page = fetch_older(pr, cursor)
        if page is None:
            return None, None


def _older_comments(pr, cursor):
    try:
        d = gh(OLDER_COMMENTS, id=pr["id"], before=cursor)
    except GhError:
        return None
    return (d.get("node") or {}).get("comments")


# Forward paging that ends. A cursor handed back twice, or more pages than
# anything real has, means the walk is not advancing; a run's exit path must
# not be held by it, and each call answering inside its deadline does not help.
MAX_PAGES = 100


def pages(fetch):
    """Yield each page of a paginated query. fetch(after) -> the connection."""
    after, seen = None, set()
    while True:
        d = fetch(after)
        yield d
        info = d.get("pageInfo") or {}
        after = info.get("endCursor")
        if not info.get("hasNextPage"):
            return
        if not after or after in seen or len(seen) >= MAX_PAGES:
            raise GhError("pagination is not advancing (cursor %r)" % after)
        seen.add(after)


def reviewed_prs(owners, accounts):
    """Every open labelled PR we have posted a verdict on, by repo#number."""
    label = "label:" + ",".join(LABELS)
    out = {}
    for owner in owners:
        def fetch(after, owner=owner):
            v = {"q": "is:pr is:open org:%s %s" % (owner, label)}
            if after:
                v["after"] = after
            return gh(SEARCH, **v)["search"]
        for d in pages(fetch):
            for pr in d["nodes"]:
                if not pr:
                    continue
                key = "%s#%d" % (pr["repository"]["nameWithOwner"], pr["number"])
                verdict, how = verdict_of(pr, accounts)
                if verdict:
                    out[key] = {"id": pr["id"], "verdict": verdict, "how": how,
                                "url": pr["url"], "title": pr["title"],
                                "author": (pr.get("author") or {}).get("login") or ""}
    return out


# --- the project --------------------------------------------------------------

FIND = """
query($owner: String!) {
  organization(login: $owner) {
    id
    projectsV2(first: 100) { nodes { id number title url } }
  }
}"""

FIELDS = """
query($project: ID!) {
  node(id: $project) { ... on ProjectV2 {
    fields(first: 50) { nodes {
      ... on ProjectV2FieldCommon { id name }
      ... on ProjectV2SingleSelectField { id name options { id name } }
    } }
  } }
}"""

VIEWS = """
query($project: ID!) {
  node(id: $project) { ... on ProjectV2 {
    views(first: 20) { nodes { id number name } }
  } }
}"""

UPDATE_VIEW = """
mutation($view: ID!, $fields: [ID!]!) {
  updateProjectV2View(input: {viewId: $view, configuration: {visibleFieldIds: $fields}}) {
    projectV2View { id name }
  }
}"""

ITEMS = """
query($project: ID!, $after: String) {
  node(id: $project) { ... on ProjectV2 {
    items(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        content {
          ... on PullRequest { id number state repository { nameWithOwner } }
          ... on Issue { id number repository { nameWithOwner } }
        }
        fieldValues(first: 30) { nodes {
          ... on ProjectV2ItemFieldSingleSelectValue { name field {
            ... on ProjectV2FieldCommon { name } } }
          ... on ProjectV2ItemFieldTextValue { text field {
            ... on ProjectV2FieldCommon { name } } }
        } }
      }
    }
  } }
}"""

# One PR already on the board, asked about directly - by node id, which a
# repository rename or transfer does not change, where owner/repo/number could
# name a different PR by the time it is asked. Absence from the label search
# is not evidence of anything: the label may simply have come off, search is
# capped at 1000 results, its index lags, and a transient failure looks
# identical to "all merged". The same shape as a search node, so its verdict
# is read the same way.
BOARD_PR = """
query($id: ID!) {
  node(id: $id) { ... on PullRequest {
    id number title url state
    author { login }
    repository { nameWithOwner }
    comments(last: 100) {
      pageInfo { hasPreviousPage startCursor }
      nodes { author { login } body }
    }
  } }
}"""

CREATE_PROJECT = """
mutation($owner: ID!, $title: String!) {
  createProjectV2(input: {ownerId: $owner, title: $title}) {
    projectV2 { id number url title }
  }
}"""

# The options are inlined rather than passed as a variable: `color` is a GraphQL
# enum, so it must appear unquoted, and gh sends -F values as strings. They are
# module constants, so there is nothing here that came from outside.
CREATE_FIELD = """
mutation($project: ID!, $name: String!) {
  createProjectV2Field(input: {
    projectId: $project, dataType: SINGLE_SELECT, name: $name,
    singleSelectOptions: [%s]
  }) { projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } } }
}"""


def _options_literal():
    return ", ".join('{name: %s, color: %s, description: %s}'
                     % (json.dumps(n), c, json.dumps(d)) for n, c, d in OPTIONS)

ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}"""

CREATE_TEXT_FIELD = """
mutation($project: ID!, $name: String!) {
  createProjectV2Field(input: {projectId: $project, dataType: TEXT, name: $name}) {
    projectV2Field { ... on ProjectV2FieldCommon { id name } }
  }
}"""

SET_TEXT = """
mutation($project: ID!, $item: ID!, $field: ID!, $text: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field, value: {text: $text}
  }) { projectV2Item { id } }
}"""

SET_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}
  }) { projectV2Item { id } }
}"""

DELETE_ITEM = """
mutation($project: ID!, $item: ID!) {
  deleteProjectV2Item(input: {projectId: $project, itemId: $item}) { deletedItemId }
}"""


def find_project(owner, title=TITLE):
    d = gh(FIND, owner=owner)["organization"]
    for p in d["projectsV2"]["nodes"]:
        if p["title"] == title:
            return d["id"], p
    return d["id"], None


def ensure_project(owner, dry=False, title=TITLE):
    owner_id, project = find_project(owner, title)
    if project is None:
        if dry:
            return None, "would create the project"
        project = gh(CREATE_PROJECT, owner=owner_id,
                     title=title)["createProjectV2"]["projectV2"]
        return project, "created"
    return project, "exists"


def ensure_field(project_id, dry=False):
    """The Result column, and the option id for each verdict."""
    for f in gh(FIELDS, project=project_id)["node"]["fields"]["nodes"]:
        if f and f.get("name") == FIELD:
            if "options" not in f:
                raise GhError("field %r exists but is not a single-select" % FIELD)
            return f["id"], {o["name"]: o["id"] for o in f["options"]}, "exists"
    if dry:
        return None, {}, "would create the field"
    d = gh(CREATE_FIELD % _options_literal(), project=project_id, name=FIELD)
    f = d["createProjectV2Field"]["projectV2Field"]
    return f["id"], {o["name"]: o["id"] for o in f["options"]}, "created"


# What the board should show. A field exists whether or not a view displays it:
# creating Result put a value on all 166 rows and showed none of them, because a
# new field is not added to views that already exist.
#
# The order here is not the order you get. visibleFieldIds is documented as
# ordered, but asking for Result, Title, Repository returns Title, Repository,
# Result - columns follow the order the fields were created in, so Result, being
# newest, sits last whatever this says.
COLUMNS = ("Title", "Repository", AUTHOR, FIELD, "Updated")


# The columns this board exists for. A view missing any of them gets it added.
OURS = (FIELD, AUTHOR)


def ensure_view(project_id, dry=False, columns=COLUMNS, ours=OURS):
    """Make every view show the columns this board is for.

    Adds what is missing to what a view already shows rather than replacing it,
    so a column someone arranged by hand survives. A view already showing all of
    ours is not touched at all.
    """
    fields = {f["name"]: f["id"]
              for f in gh(FIELDS, project=project_id)["node"]["fields"]["nodes"]
              if f and f.get("name")}
    absent = [c for c in ours if c not in fields]
    need = [fields[c] for c in ours if c in fields]
    if not need:
        return "no %s field yet" % " or ".join(ours)
    changed, skipped = [], []
    for view in gh(VIEWS, project=project_id)["node"]["views"]["nodes"]:
        shown = view_fields(project_id, view["id"])
        if shown is None:
            # One 502 while reading a customised view used to be taken as
            # permission to install the defaults, wiping whatever columns
            # somebody had arranged. If we cannot see it, we do not touch it.
            skipped.append(view["name"])
            continue
        missing = [f for f in need if f not in shown]
        if not missing:
            continue
        if dry:
            changed.append(view["name"] + " (would)")
            continue
        gh_list(UPDATE_VIEW, {"view": view["id"]}, {"fields": shown + missing})
        changed.append(view["name"])
    note = ("; %s not created yet" % ", ".join(absent)) if absent else ""
    if skipped:
        note += "; could not read %s, left alone" % ", ".join(skipped)
    return (("updated: %s" % ", ".join(changed)) if changed
            else "%s already shown" % " and ".join(c for c in ours
                                                   if c not in absent)) + note


VIEW_FIELDS = """
query($view: ID!) {
  node(id: $view) { ... on ProjectV2View {
    fields(first: 50) { nodes { ... on ProjectV2FieldCommon { id } } }
  } }
}"""


def view_fields(project_id, view_id):
    """The field ids a view currently shows, or None if it will not say."""
    try:
        nodes = gh(VIEW_FIELDS, view=view_id)["node"]["fields"]["nodes"]
    except GhError:
        return None
    return [n["id"] for n in nodes if n and n.get("id")]


def ensure_author_field(project_id, dry=False):
    """The Author column. Plain text: there are as many authors as contributors."""
    for f in gh(FIELDS, project=project_id)["node"]["fields"]["nodes"]:
        if f and f.get("name") == AUTHOR:
            return f["id"], "exists"
    if dry:
        return None, "would create the field"
    f = gh(CREATE_TEXT_FIELD, project=project_id,
           name=AUTHOR)["createProjectV2Field"]["projectV2Field"]
    return f["id"], "created"


def project_items(project_id):
    """What is on the board now, by repo#number."""
    out = {}

    def fetch(after):
        v = {"project": project_id}
        if after:
            v["after"] = after
        return gh(ITEMS, **v)["node"]["items"]

    for d in pages(fetch):
        for it in d["nodes"]:
            c = it.get("content") or {}
            repo = (c.get("repository") or {}).get("nameWithOwner")
            num = c.get("number")
            # A project can hold a free-text note, which has no content at all.
            # It is nobody's PR, so it can never match the search - skipping it
            # here is what stops it being removed as "no longer open".
            if num is None or not repo:
                continue
            key = "%s#%s" % (repo, num)
            result, author = None, None
            for fv in it["fieldValues"]["nodes"]:
                name = (fv or {}).get("field", {}).get("name")
                if name == FIELD:
                    result = fv.get("name")
                elif name == AUTHOR:
                    author = fv.get("text")
            out[key] = {"item": it["id"], "content": c.get("id"),
                        "result": result, "author": author, "state": c.get("state")}
    return out


# A sweep that returns nothing looks exactly like "everything was merged". The
# search is the only thing standing between a transient empty answer and an
# emptied board, so a removal this large has to be asked for.
PRUNE_LIMIT = 25


def too_much_to_remove(remove, present, limit=PRUNE_LIMIT):
    """True when this many removals is more likely a bad sweep than real news.

    A second line only: every removal has already been confirmed against the PR
    itself, so this exists to catch something systematically wrong rather than
    to decide individual rows.

    Scaled as well as capped - 30 rows off a board of 166 is an ordinary week,
    30 off a board of 40 is not - and it will never take the last row off a
    board that had anything on it, because "everything closed at once" is what
    a broken sweep looks like.
    """
    if not remove:
        return False
    if len(remove) >= len(present) > 1:
        return True
    return len(remove) > max(limit, len(present) // 4)


def board_pr(node_id):
    """The PR behind a board row, or None if GitHub will not say.

    None is kept: the whole point is that only a definite answer may change
    a row, and "not a PR" (an issue someone added by hand) is not a PR to
    remove either.
    """
    if not node_id:
        return None
    try:
        pr = gh(BOARD_PR, id=node_id).get("node")
    except GhError:
        return None
    if not pr or "state" not in pr:
        return None
    return pr


def refresh_board(present, wanted, accounts, fetch=board_pr):
    """Every board row the label search did not return: ask the PR itself.

    Open: it stays, with its verdict re-read from its comments, so a followup
    review reaches the board whether or not the label is still there. Closed
    or merged: gone. No answer: kept as it is. Returns (gone, kept, unsure).
    """
    gone, kept, unsure = [], [], []
    for key in sorted(k for k in present if k not in wanted):
        pr = fetch(present[key].get("content"))
        if pr is None:
            unsure.append(key)
            continue
        if pr.get("state") != "OPEN":
            gone.append(key)
            continue
        verdict, how = verdict_of(pr, accounts)
        if verdict:
            wanted[key] = {"id": pr["id"], "verdict": verdict, "how": how,
                           "url": pr.get("url"), "title": pr.get("title"),
                           "author": (pr.get("author") or {}).get("login") or ""}
        else:
            kept.append(key)            # open, nothing readable: left alone
    return gone, kept, unsure


def plan(wanted, present, keep=()):
    """What to add, what to re-label, what to drop.

    Pure, so the rules can be tested without a project: the add/update split is
    what stops every run rewriting every row, and dropping the wrong thing is
    the failure that loses work. `keep` are rows that stay exactly as they are.
    """
    add = sorted(k for k in wanted if k not in present)
    update = sorted(k for k in wanted
                    if k in present
                    and (present[k]["result"] != wanted[k]["verdict"]
                         or present[k].get("author") != wanted[k].get("author")))
    remove = sorted(k for k in present if k not in wanted and k not in keep)
    return add, update, remove


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune-only", action="store_true",
                    help="only remove what no longer belongs")
    ap.add_argument("--show", action="store_true", help="print the verdict table")
    ap.add_argument("--force-prune", action="store_true",
                    help="allow a removal large enough to look like a bad sweep")
    ap.add_argument("--owner", default="ArduPilot", help="who owns the project")
    ap.add_argument("--title", default=TITLE)
    a = ap.parse_args(argv)

    # Same list the runner posts under, newest first. Both are needed: the
    # older reviews predate the AP-Review account.
    accounts = tuple((os.environ.get("REVIEW_COMMENT_ACCOUNTS")
                      or "AP-Review tridge").split())
    owners = swept_owners()

    wanted = reviewed_prs(owners, accounts)
    if a.show:
        for k in sorted(wanted):
            print("  %-42s %-16s %s" % (k, wanted[k]["verdict"], wanted[k]["how"]))
        print("  %d reviewed open PRs" % len(wanted))
        return 0

    project, how = ensure_project(a.owner, a.dry_run, a.title)
    if project is None:
        print("%s: %s" % (a.title, how))
        return 0
    print("%s: %s  %s" % (a.title, how, project.get("url", "")))

    present = project_items(project["id"])

    # A row the label search did not return is a question, not an answer: the
    # PR itself says whether it is still open, and what its comments conclude.
    gone, kept, unsure = refresh_board(present, wanted, accounts)
    if unsure:
        print("kept %d row(s) the PR would not answer for: %s"
              % (len(unsure), ", ".join(unsure[:5])))
    add, update, remove = plan(wanted, present, keep=kept + unsure)
    if a.prune_only:
        add, update = [], []

    print("on the board: %d   reviewed and open: %d" % (len(present), len(wanted)))
    print("add %d, relabel %d, remove %d" % (len(add), len(update), len(remove)))

    if remove and too_much_to_remove(remove, present) and not a.force_prune:
        print("REFUSED: %d of %d rows would be removed. Each was confirmed closed"
              % (len(remove), len(present)))
        print("         or unlabelled, but that many at once still looks more like")
        print("         something systematically wrong. Nothing was changed.")
        print("         Re-run with --force-prune if it is real.")
        return 3

    # Only once nothing is going to be refused: fields created and a view
    # rewritten before the safety check meant "Nothing was changed" was not true.
    field_id, options, fhow = ensure_field(project["id"], a.dry_run)
    print("field %s: %s" % (FIELD, fhow))
    author_id, ahow = ensure_author_field(project["id"], a.dry_run)
    print("field %s: %s" % (AUTHOR, ahow))
    print("view: %s" % ensure_view(project["id"], a.dry_run))

    if a.dry_run:
        for k in add:
            print("  + %-42s %s" % (k, wanted[k]["verdict"]))
        for k in update:
            print("  ~ %-42s %s -> %s  author %s -> %s"
                  % (k, present[k]["result"], wanted[k]["verdict"],
                     present[k].get("author"), wanted[k].get("author")))
        for k in remove:
            print("  - %s" % k)
        return 0

    if not field_id:
        raise GhError("no %s field to write to" % FIELD)
    def write_row(item, want):
        gh(SET_FIELD, project=project["id"], item=item, field=field_id,
           option=options[want["verdict"]])
        if author_id and want.get("author"):
            gh(SET_TEXT, project=project["id"], item=item, field=author_id,
               text=want["author"])

    failed = 0
    for k in add:
        try:
            item = gh(ADD_ITEM, project=project["id"],
                      content=wanted[k]["id"])["addProjectV2ItemById"]["item"]["id"]
            write_row(item, wanted[k])
        except (GhError, KeyError) as e:
            failed += 1
            print("  ! add %s: %s" % (k, e))
    for k in update:
        try:
            write_row(present[k]["item"], wanted[k])
        except (GhError, KeyError) as e:
            failed += 1
            print("  ! relabel %s: %s" % (k, e))
    for k in remove:
        try:
            gh(DELETE_ITEM, project=project["id"], item=present[k]["item"])
        except GhError as e:
            failed += 1
            print("  ! remove %s: %s" % (k, e))
    print("done: %d added, %d relabelled, %d removed, %d failed"
          % (len(add) - failed, len(update), len(remove), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as e:
        print("FATAL: %s" % e, file=sys.stderr)
        sys.exit(2)

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
carrying a trigger label, that we have posted a verdict on. Anything else is
removed - a PR that was merged, closed, had its label taken off, or whose
review we can no longer read.

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


class GhError(Exception):
    """A GitHub call failed.

    Never swallowed: an empty answer from a failed query is indistinguishable
    from "nothing is labelled", and acting on it would empty the board.
    """


def gh(query, **variables):
    args = ["gh", "api", "graphql", "-f", "query=" + query]
    for k, v in variables.items():
        args += ["-f", "%s=%s" % (k, v)]
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout).strip()[:400])
    try:
        d = json.loads(p.stdout)
    except ValueError:
        raise GhError("unparseable reply: %s" % p.stdout[:200])
    if d.get("errors"):
        raise GhError("; ".join(e.get("message", "?") for e in d["errors"])[:400])
    return d["data"]


def gh_list(query, strings, lists):
    """gh() for a mutation that takes a list variable.

    -f sends everything as a string, so a [ID!]! has to go through --input as
    JSON rather than as repeated -f arguments.
    """
    body = {"query": query, "variables": dict(strings, **lists)}
    p = subprocess.run(["gh", "api", "graphql", "--input", "-"],
                       input=json.dumps(body), capture_output=True, text=True)
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout).strip()[:400])
    d = json.loads(p.stdout)
    if d.get("errors"):
        raise GhError("; ".join(e.get("message", "?") for e in d["errors"])[:400])
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
      comments(first: 100) { nodes { author { login } body } }
    } }
  }
}"""


def reviewed_prs(owners, accounts):
    """Every open labelled PR we have posted a verdict on, by repo#number."""
    label = "label:" + ",".join(LABELS)
    out = {}
    for owner in owners:
        after = None
        while True:
            v = {"q": "is:pr is:open org:%s %s" % (owner, label)}
            if after:
                v["after"] = after
            d = gh(SEARCH, **v)["search"]
            for pr in d["nodes"]:
                if not pr:
                    continue
                key = "%s#%d" % (pr["repository"]["nameWithOwner"], pr["number"])
                ours = our_comments(pr["comments"]["nodes"], accounts)
                verdict, how = V.of_thread(ours) if ours else (None, None)
                if verdict:
                    out[key] = {"id": pr["id"], "verdict": verdict, "how": how,
                                "url": pr["url"], "title": pr["title"],
                                "author": (pr.get("author") or {}).get("login") or ""}
            if not d["pageInfo"]["hasNextPage"]:
                break
            after = d["pageInfo"]["endCursor"]
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
          ... on PullRequest { number state repository { nameWithOwner } }
          ... on Issue { number repository { nameWithOwner } }
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
    changed = []
    for view in gh(VIEWS, project=project_id)["node"]["views"]["nodes"]:
        shown = view_fields(project_id, view["id"])
        if shown is None:
            shown, missing = [], need     # could not read it; set our own set
        else:
            missing = [f for f in need if f not in shown]
        if not missing:
            continue
        if dry:
            changed.append(view["name"] + " (would)")
            continue
        keep = shown or [fields[c] for c in columns if c in fields and fields[c] not in need]
        gh_list(UPDATE_VIEW, {"view": view["id"]}, {"fields": keep + missing})
        changed.append(view["name"])
    note = ("; %s not created yet" % ", ".join(absent)) if absent else ""
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
    out, after = {}, None
    while True:
        v = {"project": project_id}
        if after:
            v["after"] = after
        d = gh(ITEMS, **v)["node"]["items"]
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
            out[key] = {"item": it["id"], "result": result, "author": author,
                        "state": c.get("state")}
        if not d["pageInfo"]["hasNextPage"]:
            return out
        after = d["pageInfo"]["endCursor"]


# A sweep that returns nothing looks exactly like "everything was merged". The
# search is the only thing standing between a transient empty answer and an
# emptied board, so a removal this large has to be asked for.
PRUNE_LIMIT = 25


def too_much_to_remove(remove, present, limit=PRUNE_LIMIT):
    """True when this many removals is more likely a bad sweep than real news.

    Scaled as well as capped: 30 rows off a board of 166 is an ordinary week,
    30 off a board of 40 is a search that came back short.
    """
    return len(remove) > max(limit, len(present) // 4)


def plan(wanted, present):
    """What to add, what to re-label, what to drop.

    Pure, so the rules can be tested without a project: the add/update split is
    what stops every run rewriting every row, and dropping the wrong thing is
    the failure that loses work.
    """
    add = sorted(k for k in wanted if k not in present)
    update = sorted(k for k in wanted
                    if k in present
                    and (present[k]["result"] != wanted[k]["verdict"]
                         or present[k].get("author") != wanted[k].get("author")))
    remove = sorted(k for k in present if k not in wanted)
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
    field_id, options, fhow = ensure_field(project["id"], a.dry_run)
    print("field %s: %s" % (FIELD, fhow))
    author_id, ahow = ensure_author_field(project["id"], a.dry_run)
    print("field %s: %s" % (AUTHOR, ahow))
    print("view: %s" % ensure_view(project["id"], a.dry_run))

    present = project_items(project["id"])
    add, update, remove = plan(wanted, present)
    if a.prune_only:
        add, update = [], []

    print("on the board: %d   reviewed and open: %d" % (len(present), len(wanted)))
    print("add %d, relabel %d, remove %d" % (len(add), len(update), len(remove)))

    if remove and too_much_to_remove(remove, present) and not a.force_prune:
        print("REFUSED: %d of %d rows would be removed. That is more likely a short"
              % (len(remove), len(present)))
        print("         search result than that many PRs closing at once. Nothing was")
        print("         changed. Re-run with --force-prune if it is real.")
        return 3

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

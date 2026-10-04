"""Finite discovery snapshots and per-claim refresh, without GitHub mutations."""

import base64
import configparser
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import os
import re
import sys
import subprocess
from urllib.parse import quote
from zoneinfo import ZoneInfo

import repos
from review_routing import DEFAULT, owner, validate
from review_lock import acquire, canonical
import time
import threading
from urllib.request import urlopen
from urllib.error import HTTPError
from html import unescape

import review_metrics
from review_store import read as read_json

LABELS = ("DevCallTopic", "DevCallEU", "AIReview")


def module(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), Path(__file__).with_name(name + ".py")
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


POST = module("post-comments")
RULES = {
    "ardupilot": "Judge against this repository's existing conventions. ArduPilot C++: new/malloc memory is zeroed; parameter names have a 16-character limit; commits are per subsystem. Apply only where relevant. Verify claims in code and tests.",
    "fork": "Judge this fork against its own conventions and consumers, including paired repository changes.",
    "upstream": "Judge upstream compatibility and its own conventions. Never ask an upstream author to follow ArduPilot house rules.",
    "none": "Judge against this project's own conventions. ArduPilot house rules do not apply.",
}


def normal_patch(patch):
    return re.sub(
        r"^index [0-9a-f]+\.\.[0-9a-f]+.*\n",
        "",
        re.sub(r"^@@ -[0-9,]+ \+[0-9,]+ @@.*$", "@@", patch, flags=re.M),
        flags=re.M,
    )


def git(clone, *args):
    result = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, timeout=120)
    if result.returncode:
        raise OSError(result.stderr.decode(errors="replace")[:500])
    return result.stdout.decode(errors="surrogateescape").rstrip("\n")


def rebase_only(clone, old, new, base, files):
    """Step 1's own-file patch comparison, including its binary head-blob test."""
    bo, bn = git(clone, "merge-base", base, old), git(clone, "merge-base", base, new)
    if normal_patch(git(clone, "diff", bo, old, "--", *files)) != normal_patch(
        git(clone, "diff", bn, new, "--", *files)
    ):
        return False
    for name in files:
        stats = git(clone, "diff", "--numstat", bn, new, "--", name)
        if not stats.startswith("-\t"):
            continue

        def blob(commit):
            try:
                return git(clone, "rev-parse", "--verify", commit + ":" + name)
            except OSError:
                return "-"

        ob, oh, nb, nh = (blob(commit) for commit in (bo, old, bn, new))
        # A binary regenerated over a moved base is still reviewable (step 1).
        if oh != nh:
            return False
    return True


def has_commit(clone, revision):
    try:
        git(clone, "cat-file", "-e", revision + "^{commit}")
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def same_head(a, b):
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


class Discovery:
    def __init__(self, github, config, store=None):
        self.gh, self.config, self.store = github, config, store
        self.repos = {r["repo"].lower(): r for r in config.get("repos", repos.load())["repos"]}
        self.accounts = config.get("comment_accounts", [])
        self._sweep_lock = threading.RLock()
        self._clone_locks = {}
        self._swept = None
        self.unparsed = []          # manifest keys no repository claims

    def swept(self):
        with self._sweep_lock:
            if self._swept is None:
                self._swept = self._sweep()
            return self._swept

    def _sweep(self):
        cfg = self.config.get("repos", repos.load())
        main = next(r["repo"] for r in cfg["repos"] if r.get("discovery") == "main")
        try:
            response = self.gh.request(f"repos/{main}/contents/.gitmodules")
        except (OSError, TimeoutError, KeyError) as error:
            # Without it only the submodule-only repositories are missed this
            # run; a network blip here used to end the whole run.
            print("discovery: submodule sweep failed, using repos.json only: %s" % str(error)[:200],
                  file=sys.stderr)
            return {k: r for k, r in self.repos.items() if r.get("discovery") in ("main", "explicit")}
        parser = configparser.ConfigParser()
        parser.read_string(base64.b64decode(response["content"]).decode())
        owners = {o.lower() for o in cfg.get("submodule_sweep", {}).get("owners", ["ArduPilot"])}
        for section in parser.sections():
            match = re.search(
                r"github.com[:/]([^/]+/[^/]+?)(?:\.git)?$", parser.get(section, "url", fallback="")
            )
            if match and match[1].split("/")[0].lower() in owners:
                repo = match[1]
                self.repos.setdefault(
                    repo.lower(),
                    dict(
                        repo=repo,
                        key=repo.split("/")[1],
                        discovery="explicit",
                        post_comments=True,
                        house_rules="ardupilot",
                        notes="ArduPilot-owned submodule.",
                    ),
                )
        return {k: r for k, r in self.repos.items() if r.get("discovery") in ("main", "explicit")}

    MANIFEST_AGE = 120

    def manifests(self, mode=None):
        """Frozen imported manifests plus accepted store membership, built
        once per mode and reused briefly: each admission refresh asked for
        them, and rebuilding meant reading the bundle chain of every PR on
        every label page, seconds of CPU that threads could not share."""
        # one thread builds; the others in a parallel prefetch wait for it
        with self._sweep_lock:
            cache = self.__dict__.setdefault("_manifest_cache", {})
            hit = cache.get(mode)
            if hit and time.monotonic() - hit[0] < self.MANIFEST_AGE:
                return hit[1]
            built = self._manifests(mode)
            cache[mode] = (time.monotonic(), built)
            return built

    def _manifests(self, mode=None):
        if self.gh is not None and mode != "rsync":
            # submodule repositories' keys (mavlink-523) resolve only after the
            # sweep; a resumed run reaches here without discover()
            try:
                self.swept()
            except Exception as error:
                print("manifest: submodule sweep failed: " + str(error), file=sys.stderr)
        manifests = {label: dict(rows) for label, rows in self.config.get("manifests", {}).items()}
        urls = dict(self.config.get("manifest_urls", {}))
        endpoint = self.config.get("endpoints", {}).get(self.config.get("endpoint", "review"), {})
        if endpoint.get("url"):
            for label in LABELS:
                urls.setdefault(
                    label,
                    endpoint["url"].rstrip("/") + "/" + self.destination(label)[0].split("/", 1)[1],
                )
        if mode == "rsync":
            urls = (
                {"rsync": endpoint["url"].rstrip("/") + "/RsyncReviews/index.html"}
                if endpoint.get("url")
                else {}
            )
            manifests = {k: v for k, v in manifests.items() if k == "rsync"}
        elif mode and mode.startswith("@") and endpoint.get("url"):
            urls[mode] = (
                endpoint["url"].rstrip("/") + "/UserReviews/" + quote(mode[1:], safe="") + ".html"
            )
        # One fetch per page per discovery: every admitted PR refreshes its
        # candidate, and the AIReview page is two megabytes.
        if not hasattr(self, "_pages"):
            self._pages = {}
        for label, url in urls.items():
            base = (endpoint.get("url") or "").rstrip("/") + "/"
            if url not in self._pages and self.store and endpoint.get("url") and url.startswith(base) \
                    and not re.search(r"[?#]", url):
                # our own page, as its last confirmed upload to this endpoint
                # left it: no fetch; anything doubtful is fetched as before
                from review_render import served_copy
                path = url[len(base):]
                try:
                    local = served_copy(self.store, "page:%s/%s" % (self.config.get("endpoint", "review"), path),
                                        [endpoint.get("publish") or os.environ.get("REVIEW_PUBLISH"), url])
                except ValueError:
                    local = None
                if local is not None:
                    review_metrics.count("local", "manifest from the store")
                    self._pages[url] = self.parse_manifest(local.decode())
            if url not in self._pages:
                try:
                    with review_metrics.timed("site", "manifest fetch"), urlopen(url, timeout=20) as response:
                        raw = response.read(16 * 1024 * 1024 + 1)
                except HTTPError as error:
                    if error.code != 404:
                        raise
                    raw = None
                if raw is not None and len(raw) > 16 * 1024 * 1024:
                    raise OSError("manifest page exceeds bound")
                self._pages[url] = self.parse_manifest(raw.decode()) if raw is not None else None
            if self._pages[url] is not None:
                manifests.setdefault(label, {}).update(self._pages[url])
        if self.store:
            from review_store import read, digest

            for label in ["rsync"] if mode == "rsync" else LABELS:
                target = (
                    ("page:" + self.config.get("endpoint", "review") + "/RsyncReviews/index.html")
                    if label == "rsync"
                    else self.destination(label)[0]
                )
                for pr, row in read(
                    self.store.root / "membership" / (digest(target) + ".json"), {}
                ).items():
                    if row.get("removed"):
                        continue
                    bundle = (
                        next(
                            (
                                b
                                for b in self.store.chain(pr)
                                if b["generation"] == row["generation"]
                            ),
                            None,
                        )
                        if row.get("generation") is not None
                        else None
                    )
                    manifests.setdefault(label, {})[pr] = dict(
                        head=bundle["inputs"]["head"] if bundle else None,
                        section=bundle["results"]["reconciliation"]["section_md"]
                        if bundle
                        else None,
                    )
        return manifests

    def parse_manifest(self, html):
        match = re.search(r'<!-- reviewprs-manifest v1 [^>]*heads="([^"]*)"[^>]*-->', html)
        if not match:
            raise OSError("published page has no review manifest")
        names = {r.get("key", ""): repo for repo, r in self.repos.items()}
        rows = {}
        for item in unescape(match[1]).split():
            key, head = item.rsplit(":", 1)
            # The command's pages write wiki-8080 for the wiki and 34234 for
            # the main repository; the documented wiki#8080 form appears too.
            if "#" in key:
                prefix, number = key.rsplit("#", 1)
            elif key.isdecimal():
                prefix, number = "", key
            else:
                prefix, number = key.rsplit("-", 1) if "-" in key else (key, "")
            repo = names.get(prefix)
            if repo is None or not number.isdecimal() or not re.fullmatch("[0-9a-f]{7,40}", head):
                # Somebody else's page, or a slip in one: not a reason to end
                # a run that only wanted the other entries.
                self.unparsed.append(key)
                print("manifest: skipping unknown key " + key, file=sys.stderr)
                continue
            # label pages anchor rsync#1 as prrsync-1; the author page as prrsync1
            section = self.section_of(html, key.replace("#", "-")) or self.section_of(html, key.replace("#", ""))
            rows[canonical(f"pr:{repo}#{number}")] = dict(head=head, section=section, key=key)
        return rows

    @staticmethod
    def section_of(html, anchor):
        """The PR's section, in either page form.

        The new renderer writes <section id="prKEY">...</section>. The command's
        pages, which the handoff imports, write <div class="pr" id="prKEY">
        (also class "pr new" or "pr changed") and close it with a bare </div>;
        the next PR's div or the Summary heading is the only reliable end of it.
        """
        match = re.search(r'<section\b[^>]*id="pr' + re.escape(anchor) + r'"[^>]*>.*?</section>', html, re.S)
        if match:
            return match[0]
        start = re.search(r'<div class="pr(?: [^"]*)?" id="pr' + re.escape(anchor) + r'">', html)
        if not start:
            return None
        end = re.search(r'\n<div class="pr(?: [^"]*)?" id="pr|\n<h2>', html[start.end():])
        return html[start.start():start.end() + end.start()] if end else html[start.start():]

    def destination(self, label):
        endpoint = self.config.get("endpoint", "review")
        today = datetime.fromisoformat(
            self.config.get("date")
            or datetime.now(ZoneInfo("Australia/Canberra")).date().isoformat()
        ).date()
        call = today
        if label in ("DevCallTopic", "DevCallEU"):
            weekday = 1 if label == "DevCallTopic" else 2
            call += timedelta(days=(weekday - today.weekday()) % 7)
        return [
            f"page:{endpoint}/DevCallReviews/{label}/devcall_pr_reviews.html",
            # the dated archive keeps the command's DevCallReviews/2026_09_30 name:
            # it is the link handed out for a call and quoted in old comments
            f"page:{endpoint}/DevCallReviews/{call:%Y_%m_%d}/{label}/devcall_pr_reviews.html",
        ]

    def board_rows(self):
        project = self.config.get("project_id")
        if not project:
            return {}
        board = module("project-sync")
        board.gh = lambda query, **variables: self.gh.graphql(query, account="project", **variables)
        return {canonical("pr:" + key): row for key, row in board.project_items(project).items()}

    def search(self, mode, swept):
        out = set()
        for owner in sorted({repo.split("/")[0] for repo in swept}):
            selector = (
                "label:AIReview"
                if mode == "rsync"
                else (
                    "label:" + mode
                    if mode in LABELS or mode in self.config.get("labels", [])
                    else "author:" + mode.lstrip("@")
                )
            )
            query = f"is:pr is:open org:{owner} {selector}"
            for item in self.gh.pages("search/issues?q=" + quote(query), field="items"):
                repo = item["repository_url"].split("/repos/")[-1].lower()
                if repo in swept:
                    out.add(canonical(f"pr:{repo}#{item['number']}"))
        return out

    def resolve(self, argument):
        value = argument.strip()
        reserved = value.lstrip("/-").lower()
        if not value or reserved == "all":
            return "all"
        if not value.startswith("@") and reserved in ("followup", "rsync"):
            return reserved
        match = re.match(r"https://github.com/([^/]+/[^/]+)/pull/(\d+)(?:[/?#]|$)", value)
        if match:
            return match[1] + "#" + match[2]
        match = re.fullmatch(r"(?:(.*?)#)?(\d+)", value)
        if match:
            repo, number = match.groups()
            if not repo:
                repo = next(r["repo"] for r in self.repos.values() if r.get("discovery") == "main")
            elif "/" not in repo:
                choices = [r["repo"] for r in self.repos.values() if r.get("key") == repo]
                if not choices:
                    choices = [
                        r["repo"] for r in self.repos.values() if r["repo"].split("/")[1] == repo
                    ]
                if len(choices) != 1:
                    raise ValueError("unknown or ambiguous PR repository")
                repo = choices[0]
            return repo + "#" + number
        if value.startswith("@"):
            return "@" + self.gh.request("users/" + quote(value[1:], safe=""))["login"]
        known = [*LABELS, *self.config.get("labels", [])]
        label = next((label for label in known if label.lower() == value.lower()), None)
        if label:
            return label
        main = next(r["repo"] for r in self.repos.values() if r.get("discovery") == "main")
        labels = self.gh.pages(f"repos/{main}/labels")
        label = next((r["name"] for r in labels if r["name"].lower() == value.lower()), None)
        if label:
            self.config.setdefault("labels", []).append(label)
            return label
        return "@" + self.gh.request("users/" + quote(value, safe=""))["login"]

    BATCH = 50

    def batch_heads(self, prs, deadline=None):
        """Head, state, draft flag and labels of many PRs in one GraphQL query
        per repository and fifty PRs, instead of several REST reads apiece.
        A PR whose answer is missing or incomplete is left out."""
        by_repo = {}
        for pr in prs:
            repo, number = pr[3:].split("#")
            by_repo.setdefault(repo, []).append(int(number))
        out = {}
        for repo, numbers in by_repo.items():
            owner, name = repo.split("/")
            for i in range(0, len(numbers), self.BATCH):
                if deadline and time.monotonic() >= deadline:
                    return out
                part = numbers[i:i + self.BATCH]
                fields = " ".join(
                    "p%d: pullRequest(number: %d) { number state isDraft headRefOid baseRefName "
                    "labels(first: 50) { nodes { name } pageInfo { hasNextPage } } }" % (n, n) for n in part)
                query = 'query { rateLimit { cost } repository(owner: "%s", name: "%s") { %s } }' % (owner, name, fields)
                try:
                    data = self.gh.graphql(query, account="read", deadline=deadline)
                    review_metrics.count("github-points", "graphql",
                                         n=int((data.get("rateLimit") or {}).get("cost", 0)))
                    nodes = list((data.get("repository") or {}).values())
                except Exception as error:     # a failed batch costs only its PRs
                    review_metrics.count("local", "shadow batch failed")
                    print("discovery: shadow batch failed: %s" % str(error)[:200], file=sys.stderr)
                    continue
                for node in nodes:
                    try:
                        if not node or node["labels"]["pageInfo"]["hasNextPage"]:
                            continue
                        out[canonical("pr:%s#%d" % (repo, node["number"]))] = dict(
                            head=str(node["headRefOid"]), state=str(node["state"]), draft=bool(node["isDraft"]),
                            base=node["baseRefName"], labels=[str(x["name"]) for x in node["labels"]["nodes"]])
                    except (KeyError, TypeError, ValueError):
                        continue
        return out

    def predict(self, pr, heads):
        """What the batched read alone says about a followup PR: gone, the
        head we last reviewed, or moved. None when it cannot say."""
        h = heads.get(pr)
        if not h:
            return None
        if h["state"] != "OPEN" or (h["draft"] and "AIReview" not in h["labels"]):
            return "DROPPED"
        told = self.told_locally(pr)
        if not told:
            return None
        # a moved head needs the full read, whatever it then decides
        return "REUSE" if same_head(h["head"], told) else "FETCH"

    def told_locally(self, pr):
        """The head our last posted comment named, from local receipts: that
        of the newest generation whose comment was posted. A legacy review's
        comment, or one held or not yet sent, says nothing."""
        if not self.store:
            return None
        # the current review only: walking the history hashes every bundle
        bundle = self.store.bundle(pr)
        if not bundle or bundle.get("legacy"):
            return None
        for intent in bundle["intents"]:
            if intent["kind"] == "comment":
                receipt = read_json(self.store.root / "receipts" / (intent["id"] + ".json"), {})
                if receipt.get("state") == "posted":
                    return bundle["inputs"].get("head")
        return None

    SHADOW_SECONDS = 120

    @staticmethod
    def stage(candidate):
        """Discovery's decision at the same stage as the prediction: settled
        from the head alone, or needing the full read (a review, a deferral,
        or reuse found only by comparing diffs)."""
        if candidate["classification"] == "REUSE" and candidate.get("reason") == "head already told":
            return "REUSE"
        if candidate["classification"] == "DROPPED" and candidate.get("reason") != "no AI comment with told-head":
            return "DROPPED"
        return "FETCH"          # including a drop that only the thread could decide

    def shadow(self, wanted, out):
        """Run the batched read beside the per-PR reads and count where its
        prediction agrees, so it can replace them once it is shown right.
        Nothing here may fail or slow discovery: every error is counted and
        the whole is bounded in time."""
        started = time.monotonic()
        deadline = started + self.SHADOW_SECONDS
        try:
            heads = self.batch_heads(wanted, deadline)
        except Exception as error:
            review_metrics.count("local", "shadow failed")
            print("discovery: shadow failed: %s" % str(error)[:200], file=sys.stderr)
            return
        for c in out:
            if time.monotonic() >= deadline:
                review_metrics.count("local", "shadow out of time")
                break
            try:
                predicted = self.predict(c["pr"], heads)
                actual = self.stage(c)
            except Exception:
                review_metrics.count("local", "shadow error")
                continue
            if predicted is None:
                review_metrics.count("local", "shadow unknown" if c["pr"] in heads else "shadow unread")
            elif predicted == actual:
                review_metrics.count("local", "shadow agree")
            else:
                review_metrics.count("local", "shadow disagree %s->%s" % (predicted, actual))
                print("discovery: shadow predicted %s for %s, reads said %s (%s)"
                      % (predicted, c["pr"], actual, c.get("reason")), file=sys.stderr)
        review_metrics.count("local", "shadow", seconds=time.monotonic() - started)

    def discover(self, mode):
        ticket = self.store.ticket() if self.store else 0
        mode = self.resolve(mode)
        if mode != "rsync":
            self.swept()
        manifests = self.manifests(mode)
        pr_match = re.fullmatch(r"(?:https://github.com/)?([^/]+/[^/#]+)(?:/pull/|#)(\d+)", mode)
        if mode == "followup":
            keys = set(self.board_rows()) | {p for rows in manifests.values() for p in rows}
        elif pr_match:
            keys = {canonical(f"pr:{pr_match[1]}#{pr_match[2]}")}
            mode = "pr"
        else:
            swept = (
                {"rsyncproject/rsync": self.repos["rsyncproject/rsync"]}
                if mode == "rsync"
                else self.swept()
            )
            keys = self.search(mode, swept)
            if mode == "rsync" and not keys:
                return []
            if mode in LABELS or mode == "rsync":
                keys |= set(manifests.get(mode, {}))
        if mode == "followup" and self.store:
            # Only PRs we worked on recently: everything older would cost its
            # GitHub reads every followup, for ever. Decided locally; a PR
            # outside the window comes back when it is labelled or asked for.
            # A followup interval of overlap, so a push just before a PR
            # leaves the window is still seen.
            # measured from the last followup discovery that completed, so a
            # failed or interrupted one never moves the window past a PR it
            # did not look at
            days = float(self.config.get("followup_days", 14))
            started = time.time()
            covered = read_json(self.store.root / "followup-coverage.json", {}).get("at", started)
            cutoff = min(started, covered) - days * 86400 - 6 * 3600
            recent = {pr for pr in keys if self.store.in_followup_window(pr, cutoff)}
            review_metrics.count("local", "followup outside window", n=len(keys) - len(recent))
            keys = recent
        routing = validate(self.config.get("routing", DEFAULT))
        wanted = []
        for pr in sorted(keys):
            repository = pr[3:].split("#")[0]
            if owner(routing, mode, repository) != "new":
                continue
            # During a repository canary, shared label/author destinations
            # still belong to old publication. Do not partially overwrite them.
            if mode not in ("pr", "rsync") and owner(routing, mode) != "new":
                continue
            wanted.append(pr)
        # Each candidate is a handful of independent GitHub round trips; one
        # at a time, 170 PRs took a quarter of an hour.
        workers = int(self.config.get("discovery_workers", 8))

        def one(pr):
            # one PR GitHub will not describe costs that PR this run, not
            # the run: it keeps its rows and the next run picks it up
            try:
                return self.candidate(pr, mode, manifests)
            except (OSError, TimeoutError, KeyError, subprocess.TimeoutExpired) as error:
                print("discovery: skipping %s: %s" % (pr, str(error)[:200]), file=sys.stderr)
                self.skipped.append(pr)
                if mode == "followup" and self.store:
                    # unread, it may have changed: keep it in the window
                    self.store.mark_pending(pr, "discovery error")
                return None

        self.skipped = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            out = [c for c in pool.map(one, wanted) if c is not None]
        for candidate in out:
            candidate["observation"] = ticket
        if mode == "followup" and self.config.get("shadow_batch", True):
            self.shadow(wanted, out)
        if mode == "followup" and self.store:
            # the controller records coverage once these candidates are
            # durable in its run; recording it here could lose a review to a
            # crash before then
            self.followup_started = started
        if mode == "followup" and not any(c["classification"] == "REVIEW" for c in out):
            for c in out:
                c["destinations"] = []
        return sorted(out, key=lambda c: (c["created_at"], c["pr"]))

    def candidate(self, pr, mode, manifests=None):
        read_at = time.time()
        result = self._candidate(pr, mode, manifests)
        # when this PR was read, for admission to judge the read's age
        result["discovered_at"] = read_at
        return result

    def _candidate(self, pr, mode, manifests=None):
        manifests = self.manifests(mode) if manifests is None else manifests
        repo, number = pr[3:].split("#")
        number = int(number)
        meta = self.gh.request(f"repos/{repo}/pulls/{number}")
        repo = meta["base"]["repo"]["full_name"].lower()
        pr = canonical(f"pr:{repo}#{number}")
        info = self.repos.get(repo)
        if info is None:
            info = dict(
                repo=repo,
                key=repo.split("/")[1],
                house_rules="ardupilot" if repo.startswith("ardupilot/") else "none",
                post_comments=repo.startswith("ardupilot/"),
            )
        key = (info.get("key", "") + "#" if info.get("key") else "") + str(number)
        head, base = meta["head"]["sha"], meta["base"]["sha"]
        labels = [x["name"] for x in meta.get("labels", [])]
        memberships = [label for label, rows in manifests.items() if pr in rows]
        old = manifests.get(mode, {}).get(pr, {})
        if not old and memberships:
            old = manifests[memberships[0]][pr]
        old = dict(head=old) if isinstance(old, str) else old
        # A label PR at its published head is reused whatever its thread
        # says; the thread is the costly part of a candidate (several paged
        # calls), so it is read only when the review can use it.
        # A draft is left alone unless someone asked for a review of it.
        skip_draft = meta["draft"] and "AIReview" not in labels
        reuse = (
            (mode in LABELS or mode == "rsync")
            and meta["state"] == "open"
            and not skip_draft
            and ("AIReview" if mode == "rsync" else mode) in labels
            and same_head(head, old.get("head"))
        )
        if reuse:
            thread = []
        elif mode == "followup":
            # Our last comment, and the head it reviewed, are in the issue
            # comments alone. Reviews and inline comments are only fetched
            # for a PR that has moved on and will be reviewed: reading all
            # three for every PR made a followup's discovery take minutes.
            thread = self.gh.thread(repo, number, kinds=("comment",))
            told = [c for c in thread if c["login"] in self.accounts and POST.MARKER in c["body"]]
            last = max(told, key=lambda c: (c["at"] or "", c["id"])) if told else None
            if last and not same_head(head, POST.told_head(last["body"])):
                thread += self.gh.thread(repo, number, kinds=("review_comment", "review"))
        else:
            thread = self.gh.thread(repo, number)
        ours = [
            c
            for c in thread
            if c["kind"] == "comment" and c["login"] in self.accounts and POST.MARKER in c["body"]
        ]
        previous = dict(max(ours, key=lambda c: (c["at"] or "", c["id"]))) if ours else None
        if previous:
            previous["told_head"] = POST.told_head(previous["body"])
            # Legacy comments have no machine-readable finding IDs. Retain every
            # prose paragraph as an explicit previous-round obligation, rather than
            # dropping findings whose author did not use BUG/ISSUE headings.
            prose = "\n".join(previous["body"].splitlines()[3:])
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", prose) if p.strip()]
            previous["findings"] = [
                {"id": f"previous:{previous['id']}:{i}", "claim": paragraph}
                for i, paragraph in enumerate(paragraphs or [previous["body"]])
            ]
        destinations = [
            target for label in memberships if label in LABELS
            and owner(self.config.get("routing", DEFAULT), label) == "new"
            for target in self.destination(label)
        ]
        endpoint = self.config.get("endpoint", "review")
        if mode in LABELS or mode in self.config.get("labels", []):
            destinations += self.destination(mode)
        elif mode == "rsync":
            destinations += [f"page:{endpoint}/RsyncReviews/index.html"]
        elif mode == "pr":
            prefix = self.config.get("retained_prefix", "PRReviews")
            destinations += [f"page:{endpoint}/{prefix}/{repo}/{number}/index.html"]
        elif mode == "followup":
            destinations += [
                f"page:{endpoint}/DevCallReviews/followups/{self.config['stamp']}/devcall_pr_reviews.html"
            ]
        else:
            destinations += [f"page:{endpoint}/UserReviews/{quote(mode.lstrip('@'), safe='')}.html"]
        candidate = dict(
            pr=pr,
            repository=repo,
            number=number,
            node_id=meta["node_id"],
            manifest_key=key,
            head=head,
            base=base,
            merge_base=base,
            created_at=meta["created_at"],
            title=meta["title"],
            author=meta["user"]["login"],
            labels=labels,
            draft=meta["draft"],
            open=meta["state"] == "open",
            ci=self.ci(repo, head),
            previous_comment=previous,
            previous_section=old.get("section"),
            previous_manifest_head=old.get("head"),
            rules=RULES[info["house_rules"]] + "\n" + info.get("notes", ""),
            destinations=list(dict.fromkeys(destinations)),
            mode=mode,
            thread=thread,
            post=bool(
                info.get("post_comments")
                and (
                    mode in (*LABELS, "rsync", "followup", "pr")
                    or self.config.get("authorize_post")
                )
            ),
            held=not info.get("post_comments"),
            classification="REVIEW",
            reason="new or changed head",
            configuration=self.config,
        )

        candidate["membership_removed"] = {}
        for target in candidate["destinations"]:
            label = next(
                (
                    label
                    for label in (*LABELS, *self.config.get("labels", []))
                    if "/" + label + "/" in target
                ),
                None,
            )
            candidate["membership_removed"][target] = (
                not candidate["open"]
                or skip_draft
                or (label is not None and label not in labels)
            )

        def classify(value, reason):
            candidate.update(classification=value, reason=reason)
            return candidate

        if not candidate["open"]:
            return classify("DROPPED", "closed or merged")
        if skip_draft:
            return classify("DROPPED", "draft without AIReview")
        if (mode in LABELS or mode == "rsync") and (
            "AIReview" if mode == "rsync" else mode
        ) not in labels:
            return classify("DROPPED", "label removed")
        current = self.store.bundle(pr) if self.store else None
        if current and current["inputs"]["head"] == head:
            candidate["merge_base"] = current["inputs"]["merge_base"]
        if mode == "followup":
            if not previous or not previous["told_head"]:
                return classify("DROPPED", "no AI comment with told-head")
            if same_head(head, previous["told_head"]):
                return classify("REUSE", "head already told")
        elif mode != "pr" and same_head(head, old.get("head")):
            return classify("REUSE", "manifest head unchanged")
        try:
            self.snapshot_diff(candidate, previous["told_head"] if mode == "followup" else None)
        except (OSError, subprocess.TimeoutExpired) as error:
            return classify("DEFERRED", "diff unavailable: " + str(error))
        return candidate

    def ci(self, repo, head):
        observed = datetime.now(ZoneInfo("UTC")).isoformat()
        try:
            status = self.gh.request(f"repos/{repo}/commits/{head}/status")
            checks = self.gh.pages(f"repos/{repo}/commits/{head}/check-runs", field="check_runs")
            states = [x["state"] for x in status.get("statuses", [])]
            states += [
                "pending"
                if x["status"] != "completed"
                else (
                    "success" if x["conclusion"] in ("success", "neutral", "skipped") else "failure"
                )
                for x in checks
            ]
            value = (
                "failing"
                if any(s in ("failure", "error") for s in states)
                else "pending"
                if "pending" in states
                else "passing"
                if states
                else "none"
            )
        except OSError:
            value = "unknown"
        return dict(state=value, head=head, at=observed)

    def snapshot_diff(self, candidate, old=None):
        clone = self.config.get("reference_clones", {}).get(candidate["repository"])
        if not clone:
            raise OSError("no reference clone configured")
        # candidates are discovered in parallel; git fetches into one clone
        # are not, and FETCH_HEAD is not written under a lock
        with self._sweep_lock:
            gate = self._clone_locks.setdefault(clone, threading.Lock())
        with gate:
            self._snapshot_diff(clone, candidate, old)

    def _snapshot_diff(self, clone, candidate, old):
        deadline = time.monotonic() + 120
        lock = acquire(self.store.locks, "refresh", deadline, shared=True) if self.store else None
        try:
            if self.store and lock is None:
                raise TimeoutError("refresh busy")
            # Fetches add objects/refs; they never check out the mutable base.
            # A commit the clone already has needs no fetch.
            for revision in dict.fromkeys([candidate["head"], candidate["base"]]):
                if not has_commit(clone, revision):
                    with review_metrics.timed("github", "git fetch"):
                        git(clone, "fetch", "--no-tags", "origin", revision)
            if old:
                old = self.told_commit(clone, candidate["repository"], old)
            candidate["merge_base"] = git(clone, "merge-base", candidate["base"], candidate["head"])
            # The files GitHub lists for a PR are those changed between the
            # merge base and the head, which the clone gives without a call or
            # the files endpoint's 3000-file cap; the API is the fallback.
            try:
                files = [f for f in git(clone, "diff", "--name-only", "-z", candidate["merge_base"],
                                        candidate["head"]).split("\0") if f]
                review_metrics.count("local", "files from git")
            except OSError:
                files = [
                    x["filename"]
                    for x in self.gh.pages(
                        f"repos/{candidate['repository']}/pulls/{candidate['number']}/files"
                    )
                ]
            candidate["diff"] = git(
                clone, "diff", candidate["merge_base"], candidate["head"], "--", *files
            )
            if old and rebase_only(clone, old, candidate["head"], candidate["base"], files):
                candidate.update(classification="REUSE", reason="rebase-only")
        finally:
            if lock:
                lock.close()

    def told_commit(self, clone, repository, told):
        """The full commit a comment's told head names, fetched, or None.
        Comments carry ten hex digits and a server will not fetch by
        abbreviation. A told head that is gone (force-pushed and collected)
        only loses the rebase-only shortcut; it is no reason to defer."""
        try:
            return git(clone, "rev-parse", "--verify", "--quiet", told + "^{commit}")
        except OSError:
            pass
        try:
            full = self.gh.request(f"repos/{repository}/commits/{told}")["sha"]
            with review_metrics.timed("github", "git fetch"):
                git(clone, "fetch", "--no-tags", "origin", full)
            return full
        except (OSError, KeyError, TypeError, subprocess.TimeoutExpired):
            return None

    def refresh(self, candidate):
        ticket = self.store.ticket() if self.store else 0
        fresh = self.candidate(candidate["pr"], candidate["mode"])
        choices = candidate.get("selection_modes", [])
        if fresh["reason"] == "label removed":
            mode = next((mode for mode in choices if mode in fresh["labels"]), None)
            if mode:
                fresh = self.candidate(candidate["pr"], mode)
        fresh["selection_modes"] = choices
        fresh["observation"] = ticket
        return fresh

"""Finite discovery snapshots and per-claim refresh, without GitHub mutations."""

import base64
import configparser
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import re
import subprocess
from urllib.parse import quote
from zoneinfo import ZoneInfo

import repos
from review_lock import acquire, canonical
import time
import threading
from urllib.request import urlopen
from urllib.error import HTTPError
from html import unescape

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


def same_head(a, b):
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


class Discovery:
    def __init__(self, github, config, store=None):
        self.gh, self.config, self.store = github, config, store
        self.repos = {r["repo"].lower(): r for r in config.get("repos", repos.load())["repos"]}
        self.accounts = config.get("comment_accounts", [])
        self._sweep_lock = threading.RLock()
        self._swept = None

    def swept(self):
        with self._sweep_lock:
            if self._swept is None:
                self._swept = self._sweep()
            return self._swept

    def _sweep(self):
        cfg = self.config.get("repos", repos.load())
        main = next(r["repo"] for r in cfg["repos"] if r.get("discovery") == "main")
        response = self.gh.request(f"repos/{main}/contents/.gitmodules")
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

    def manifests(self, mode=None):
        """Frozen imported manifests plus accepted store membership."""
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
        for label, url in urls.items():
            try:
                with urlopen(url, timeout=20) as response:
                    raw = response.read(16 * 1024 * 1024 + 1)
            except HTTPError as error:
                if error.code == 404:
                    continue
                raise
            if len(raw) > 16 * 1024 * 1024:
                raise OSError("manifest page exceeds bound")
            manifests.setdefault(label, {}).update(self.parse_manifest(raw.decode()))
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
                        if row.get("generation")
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
            prefix, number = key.rsplit("#", 1) if "#" in key else ("", key)
            repo = names.get(prefix)
            if repo is None or not number.isdecimal() or not re.fullmatch("[0-9a-f]{7,40}", head):
                raise OSError("unknown manifest key or head: " + key)
            section = re.search(
                r'<section\b[^>]*id="pr'
                + re.escape(key.replace("#", "-"))
                + r'"[^>]*>.*?</section>',
                html,
                re.S,
            )
            rows[canonical(f"pr:{repo}#{number}")] = dict(
                head=head, section=section[0] if section else None
            )
        return rows

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
            f"page:{endpoint}/DevCallReviews/{call}/{label}/devcall_pr_reviews.html",
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
        out = []
        for pr in sorted(keys):
            candidate = self.candidate(pr, mode, manifests)
            candidate["observation"] = ticket
            out.append(candidate)
        if mode == "followup" and not any(c["classification"] == "REVIEW" for c in out):
            for c in out:
                c["destinations"] = []
        return sorted(out, key=lambda c: (c["created_at"], c["pr"]))

    def candidate(self, pr, mode, manifests=None):
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
        memberships = [label for label, rows in manifests.items() if pr in rows]
        old = manifests.get(mode, {}).get(pr, {})
        if not old and memberships:
            old = manifests[memberships[0]][pr]
        old = dict(head=old) if isinstance(old, str) else old
        destinations = [
            target for label in memberships if label in LABELS for target in self.destination(label)
        ]
        endpoint = self.config.get("endpoint", "review")
        if mode in LABELS or mode in self.config.get("labels", []):
            destinations += self.destination(mode)
        elif mode == "rsync":
            destinations += [f"page:{endpoint}/RsyncReviews/index.html"]
        elif mode == "pr":
            destinations += [f"page:{endpoint}/PRReviews/{repo}/{number}/index.html"]
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
                or candidate["draft"]
                or (label is not None and label not in labels)
            )

        def classify(value, reason):
            candidate.update(classification=value, reason=reason)
            return candidate

        if not candidate["open"]:
            return classify("DROPPED", "closed or merged")
        if candidate["draft"]:
            return classify("DROPPED", "draft")
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
        deadline = time.monotonic() + 120
        lock = acquire(self.store.locks, "refresh", deadline, shared=True) if self.store else None
        try:
            if self.store and lock is None:
                raise TimeoutError("refresh busy")
            # Fetches add objects/refs; they never check out the mutable base.
            for revision in dict.fromkeys(
                [candidate["head"], candidate["base"], *([old] if old else [])]
            ):
                git(clone, "fetch", "--no-tags", "origin", revision)
            candidate["merge_base"] = git(clone, "merge-base", candidate["base"], candidate["head"])
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

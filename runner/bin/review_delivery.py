"""Publication, comment and board leaves of the durable outbox protocol."""

from contextlib import ExitStack
import os
from pathlib import Path
import re
import sys
import shlex
import subprocess
import time
from urllib.request import urlopen, Request
from urllib.parse import quote

from review_discovery import LABELS, POST, module
import review_metrics
from review_lock import acquire, canonical, region
from review_render import Renderer, anchor, bundle_at, sha, verify
from review_store import Store, atomic, create_once, digest, mkdir, read

# Line 1 of every comment, verbatim what the command writes today: the tools
# match "AI-generated" and readers know the sentence.
MARKER = ("**Automated review note — AI-generated (Claude+Codex), validated against the "
          "live diff.** Please sanity-check before acting.")


def delivery_marker(ident):
    if not re.fullmatch("[0-9a-f]{64}", ident):
        raise ValueError("invalid delivery id")
    return "<!-- apreview-delivery:v1:" + ident + " -->"


def comment_body(entry, bundle, url, observed):
    final, head = bundle["results"]["reconciliation"], bundle["inputs"]["head"]
    verdict = "" if entry["kind"] == "note" else "**Verdict: " + final["verdict"] + "**"
    body = (
        "\n".join(
            [
                MARKER,
                verdict,
                delivery_marker(entry["id"]),
                # ten hex characters, the form every told-head reader expects,
                # including the old followup's sed until cutover
                "Reviewed at head `" + head[:10] + "`.",
                "Full report: " + url,
                "",
                final["comment_md"],
            ]
        )
        + "\n"
    )
    if observed != head:
        body += (
            "\nHead moved during review: observed `"
            + observed
            + "`. This review covers only `"
            + head
            + "`; followup eligible.\n"
        )
    return body


def timeout(deadline):
    seconds = min(20, deadline - time.monotonic())
    if seconds <= 0:
        raise TimeoutError("delivery deadline")
    return seconds


def run_external(*args, **kwargs):
    try:
        return subprocess.run(*args, **kwargs)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("external delivery deadline") from error


class Publication:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.renderer = Renderer(store)

    def endpoint(self, target):
        target = canonical(target)
        endpoint, path = target[5:].split("/", 1)
        try:
            return self.config["endpoints"][endpoint], path
        except KeyError as error:
            raise OSError("publication endpoint is not configured: " + endpoint) from error

    def url(self, target):
        endpoint, path = self.endpoint(target)
        return endpoint["url"].rstrip("/") + "/" + quote(path, safe="/")

    def fetch(self, target, deadline, expected=()):
        request = Request(self.url(target), headers={"Cache-Control": "no-cache"})
        with review_metrics.timed("site", "fetch page"), urlopen(request, timeout=timeout(deadline)) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise OSError("served page exceeds bound")
        return verify(raw, expected)

    def render(self, entry, commit=False):
        """The page's bytes as the store stands, and the receipts it read.
        Nothing is saved: a landing page's routes are kept in
        self.renderer.pending until the page is really published."""
        target = entry["target"]
        endpoint, path = self.endpoint(target)
        retained = (
            bundle_at(self.store, entry["pr"], entry["generation"])
            if entry.get("retained")
            else None
        )
        if re.fullmatch(r"DevCallReviews/\d{4}[-_]\d{2}[-_]\d{2}/devcall_pr_reviews.html", path):
            date = path.split("/")[1]
            pages = {}
            revisions = {}
            consumed = []
            # Each dated label page records its latest upload in
            # landing/<endpoint>/<date>/<label>.json, written under its own
            # page lock as it is published: a handful of reads instead of
            # parsing every receipt in the store. The index is trusted only
            # once marked complete; until then the receipts are scanned and
            # any label missing from the index is backfilled, never
            # overwriting a fresher entry, and the mark written.
            endpoint_name = canonical(target)[5:].split("/", 1)[0]
            index = self.store.root / "landing" / endpoint_name / date
            complete = (index / ".complete").exists()
            indexed = {}
            for record in sorted(index.glob("*.json")) if index.is_dir() else ():
                try:
                    page = read(record)
                except (OSError, ValueError):
                    page = None
                if not (isinstance(page, dict) and Store.valid_key(page.get("target"))
                        and page["target"].startswith("page:")
                        and isinstance(page.get("state"), str)
                        and isinstance(page.get("anchors", []), list)
                        and all(isinstance(a, str) for a in page.get("anchors", []))
                        and isinstance(page.get("revision", 0), int)):
                    # a damaged record would drop its label from the page:
                    # this page waits for repair; the drain carries on
                    raise ValueError("landing: damaged index record %s" % record)
                if page.get("state") in ("published", "superseded"):
                    indexed[record.stem] = dict(path=record.stem + "/devcall_pr_reviews.html",
                                                anchors=page.get("anchors", []), target=page["target"])
                    revisions[record.stem] = page.get("revision", 0)
            pages.update(indexed)
            indexed_revisions = dict(revisions)
            scan = not complete
            # an unreadable receipt raises: this page waits, rather than being
            # published without a label the receipt may hold
            for receipt in (self.store.receipts() if scan else ()):
                # only page receipts have page keys: a board receipt targets a
                # project id, which canonical() rejects and must not end the scan.
                # A receipt that may be one of this page's labels but is damaged
                # raises: the page waits rather than going out without it.
                if not (isinstance(receipt, dict) and isinstance(receipt.get("target"), str)):
                    raise ValueError("landing: a receipt is not a record")
                where = receipt["target"]
                try:
                    where = canonical(where) if where.startswith("page:") else ""
                except ValueError:
                    where = ""
                match = re.fullmatch(
                    r"page:(" + re.escape(endpoint_name) + r")/DevCallReviews/" + date
                    + r"/([^/]+)/devcall_pr_reviews.html",
                    where,
                )
                if match and not (isinstance(receipt.get("state"), str) and isinstance(receipt.get("id"), str)
                                  and isinstance(receipt.get("revision", 0), int)
                                  and isinstance(receipt.get("anchors", []), list)
                                  and all(isinstance(a, str) for a in receipt.get("anchors", []))):
                    raise ValueError("landing: damaged receipt %s for %s" % (receipt.get("id"), match[2]))
                if match and receipt.get("state") in ("published", "superseded"):
                    # every label publish this render saw; the page then shows
                    # that label at this revision or a later one
                    consumed.append(receipt.get("id"))
                if (
                    match
                    and receipt.get("state") in ("published", "superseded")
                    and receipt.get("revision", 0) >= revisions.get(match[2], -1)
                ):
                    revisions[match[2]] = receipt.get("revision", 0)
                    pages[match[2]] = dict(
                        path=match[2] + "/devcall_pr_reviews.html",
                        anchors=receipt.get("anchors", []),
                        target=receipt["target"],
                    )
            if scan:
                # The scan took, per label, whichever of index entry and receipt
                # has the higher revision. Missing labels are backfilled, never
                # replacing an entry; the index is marked complete only when no
                # receipt is newer than its entry, which happens only if a crash
                # cut off an index write, and the label's next upload repairs.
                mkdir(index)
                for label, page in pages.items():
                    if label not in indexed:
                        create_once(index / (label + ".json"), dict(
                            target=canonical(page["target"]), state="published",
                            revision=revisions.get(label, 0), anchors=page["anchors"]))
                if all(revisions.get(label, 0) <= indexed_revisions.get(label, revisions.get(label, 0))
                       for label in pages):
                    atomic(index / ".complete", True)
            with review_metrics.timed("local", "render landing"):
                raw = self.renderer.landing(date, pages, commit=commit)
        else:
            consumed = []
            with review_metrics.timed("local", "render page"):
                raw = self.renderer.render(target, retained)
        return raw, consumed

    VERIFY_EVERY = 10
    VERIFY_AGE = 6 * 3600

    def destination(self, target):
        """Where a page goes and is served from, under this configuration."""
        endpoint, path = self.endpoint(target)
        return [endpoint.get("publish") or os.environ.get("REVIEW_PUBLISH"), self.url(target)]

    def confirmed(self, entry):
        """The page's last verified upload, if it went where this entry's
        frozen configuration sends it; another server's upload proves nothing."""
        if entry.get("retained"):
            return None
        record = read(self.store.root / "pages" / digest(entry["target"]) / "confirmed.json")
        if not record or record.get("destination") != self.destination(entry["target"]):
            return None
        return {k: v for k, v in record.items() if k != "destination"}

    def current(self, entry):
        """What the page would be now, and the revision last published. A
        render equal to an upload that is still the latest publication of the
        page proves that upload shows the store's current state. Local only."""
        directory = self.store.root / "pages" / digest(entry["target"])
        return dict(page_digest=verify(self.render(entry)[0])["page_digest"],
                    revision=read(directory / "revision.json", 0),
                    epoch=read(directory / "epoch.json", 0))

    def deliver(self, entry, deadline):
        target = entry["target"]
        endpoint, path = self.endpoint(target)
        raw, consumed = self.render(entry)
        landing = bool(re.fullmatch(r"DevCallReviews/\d{4}[-_]\d{2}[-_]\d{2}/devcall_pr_reviews.html", path))
        expected = verify(raw)
        directory = self.store.root / "pages" / digest(target)
        mkdir(directory)
        source = directory / Path(path).name
        source.write_bytes(raw)
        publish = endpoint.get("publish") or os.environ.get("REVIEW_PUBLISH")
        if not publish:
            raise OSError("no REVIEW_PUBLISH endpoint")
        # Counted before the remote effect: an upload that then fails or dies
        # may still have replaced the page, so any cached proof of an earlier
        # upload must stop counting from here.
        epoch = read(directory / "epoch.json", 0) + 1
        atomic(directory / "epoch.json", epoch)
        # rsync's normal temp-file + rename, never --inplace. One page per transfer.
        destination = publish.rstrip("/") + "/" + str(Path(path).parent) + "/"
        with review_metrics.timed("site", "rsync page"):
            result = run_external(
                # --checksum: the quick check skips a same-size change made in
                # the same second, and a sampled upload trusts the exit status
                ["rsync", "--mkpath", "--delay-updates", "--checksum", *endpoint.get("rsync_args", []),
                 "--", str(source), destination],
                capture_output=True,
                timeout=timeout(deadline),
            )
        if result.returncode:
            raise OSError("rsync failed: " + result.stderr.decode(errors="replace")[:500])
        # The fetch-back is sampled: every VERIFY_EVERY uploads of a page, or
        # when its last check is VERIFY_AGE old. A comment's own check still
        # fetches every section it links to before posting (verify_comment),
        # so what a comment points at is always seen served.
        checked = read(directory / "verified.json", {})
        if checked.get("uploads", 0) + 1 >= self.VERIFY_EVERY or time.time() - checked.get("at", 0) > self.VERIFY_AGE:
            served = self.fetch(target, deadline, expected["sections"])
            if served["page_digest"] != expected["page_digest"]:
                raise OSError("served page differs from rendered page")
            atomic(directory / "verified.json", dict(at=time.time(), uploads=0))
        else:
            served = {k: expected[k] for k in ("page_digest", "sections")}
            atomic(directory / "verified.json", dict(checked, uploads=checked.get("uploads", 0) + 1))
            review_metrics.count("local", "upload not fetched back")
        revision = read(directory / "revision.json", 0) + 1
        atomic(directory / "revision.json", revision)
        if landing:
            # routes saved only for a page that was really uploaded
            atomic(*self.renderer.pending)
        dated = re.fullmatch(r"DevCallReviews/(\d{4}[-_]\d{2}[-_]\d{2})/([^/]+)/devcall_pr_reviews.html", path)
        rows = read(self.store.root / "membership" / (digest(target) + ".json"), {})
        removed = (
            isinstance(entry["generation"], int)
            and not entry.get("retained")
            and rows.get(entry["pr"], {}).get("removed")
        )
        result = dict(
            state="superseded" if removed else "published",
            **served,
            revision=revision,
            anchors=re.findall(r'<section id="([^"]+)"', raw.decode()),
            url=self.url(target),
            consumed=consumed,
            epoch=epoch,
            # what each row put on the page this upload served
            views=None if landing or entry.get("retained") else self.renderer.views,
        )
        if dated:
            # this label's entry in its date's landing index
            atomic(self.store.root / "landing" / canonical(target)[5:].split("/", 1)[0] / dated[1]
                   / (dated[2] + ".json"),
                   dict(target=canonical(target), state=result["state"], revision=revision,
                        anchors=result["anchors"]))
        if not entry.get("retained"):
            # the page as last confirmed uploaded (fetched back when sampled):
            # a later publish whose fresh render has these bytes, with no
            # upload since, needs no upload
            atomic(directory / "confirmed.json", dict(result, destination=self.destination(target)))
        return result

    def verify_comment(self, entry, deadline):
        bundle = bundle_at(self.store, entry["pr"], entry["generation"])
        expected = dict(
            pr=entry["pr"],
            generation=entry["generation"],
            digest=sha(__import__("review_render").core(bundle).encode()),
        )
        retained_ok = False
        for intent in bundle["intents"]:
            if intent["kind"] != "publish" or intent.get("landing"):
                continue
            if not intent.get("retained"):
                rows = read(
                    self.store.root / "membership" / (digest(intent["target"]) + ".json"), {}
                )
                if rows.get(entry["pr"], {}).get("removed"):
                    continue
            try:
                self.fetch(intent["target"], deadline, [expected])
            except OSError:
                # The caller holds every required page region. Repair from the
                # authoritative store; a newer section still cannot satisfy this one.
                repair = dict(intent, pr=entry["pr"], generation=entry["generation"])
                self.deliver(repair, deadline)
                self.fetch(intent["target"], deadline, [expected])
            retained_ok |= bool(intent.get("retained"))
        if not retained_ok:
            raise OSError("retained section did not verify")


class Posting:
    def __init__(self, store, github, publication, config, now=time.time):
        self.store, self.gh, self.pages, self.config, self.now = (
            store,
            github,
            publication,
            config,
            now,
        )

    def live(self, entry, deadline):
        repo, number = entry["pr"][3:].split("#")
        live = self.gh.request(f"repos/{repo}/pulls/{number}", account="comment", deadline=deadline)
        thread = self.gh.thread(repo, int(number), account="comment", deadline=deadline)
        return live, thread

    def prepare(self, entry, deadline):
        bundle = bundle_at(self.store, entry["pr"], entry["generation"])
        inputs = bundle["inputs"]
        if inputs.get("held"):
            retained = next(i["target"] for i in bundle["intents"] if i.get("retained"))
            body = comment_body(
                entry, bundle, self.pages.url(retained) + "#" + anchor(inputs), inputs["head"]
            )
            path = self.store.root / "held" / (entry["id"] + ".md")
            mkdir(path.parent)
            atomic(path, body, encode=lambda text: text.encode("utf-8"))
            return dict(
                state="held",
                manual_command="gh pr comment "
                + shlex.quote(
                    "https://github.com/" + inputs["repository"] + "/pull/" + str(inputs["number"])
                )
                + " --body-file "
                + shlex.quote(str(path)),
            )
        if not inputs.get("post"):
            return dict(state="not_applicable")
        live, thread = self.live(entry, deadline)
        self.pages.verify_comment(entry, deadline)
        accounts = self.config.get("comment_accounts", [])
        if not accounts:
            raise OSError("no frozen comment account configured")
        retained = next(i["target"] for i in bundle["intents"] if i.get("retained"))
        body = comment_body(
            entry, bundle, self.pages.url(retained) + "#" + anchor(inputs), live["head"]["sha"]
        )
        note = entry["kind"] == "note"
        action, target = POST.decide(
            thread, body, accounts, head=inputs["head"], mode=inputs.get("mode", "label"), note=note
        )
        if action == "edit" and any(
            c["id"] == target and c["login"] != accounts[0] for c in thread
        ):
            action = "repost"
        predecessors = (
            []
            if note
            else [
                c
                for c in thread
                if c["kind"] == "comment"
                and c["login"] in accounts
                and POST.MARKER in c["body"]
                and POST.states_verdict(c["body"])
                and not c["body"].startswith(POST.DEPRECATED_PREFIX)
                and c["id"]
                != (target if action in ("edit", "unchanged", "deprecate-stale") else None)
            ]
        )
        entry["payload"] = dict(
            body=body,
            body_digest=sha(body.encode()),
            action=action,
            target_id=target,
            account=accounts[0],
            predecessors=predecessors,
            observed_head=live["head"]["sha"],
            repository=inputs["repository"],
            number=inputs["number"],
            url=self.pages.url(retained),
        )
        entry["sent_at"] = self.now()
        return None

    def check_account(self, expected, deadline):
        identity = self.gh.request("user", account="comment", deadline=deadline)
        if identity.get("login") != expected:
            raise OSError("comment identity differs from frozen account")

    def deliver(self, entry, deadline):
        payload = entry["payload"]
        if payload["action"] in ("unchanged", "deprecate-stale"):
            return self.receipt(entry, payload["target_id"])
        self.check_account(payload["account"], deadline)
        repo, number = payload["repository"], payload["number"]
        edit = payload["action"] == "edit"
        endpoint = (
            f"repos/{repo}/issues/comments/{payload['target_id']}"
            if edit
            else f"repos/{repo}/issues/{number}/comments"
        )
        response = self.gh.request(
            endpoint,
            method="PATCH" if edit else "POST",
            account="comment",
            payload={"body": payload["body"]},
            deadline=deadline,
        )
        if not response.get("id"):
            raise OSError("write returned no comment id")
        return self.receipt(entry, response["id"], response.get("html_url"))

    def receipt(self, entry, comment_id, url=None):
        return dict(
            state="posted",
            comment_id=comment_id,
            url=url,
            payload_digest=entry["payload"]["body_digest"],
            predecessors=entry["payload"]["predecessors"],
            account=entry["payload"]["account"],
        )

    def reconcile(self, entry, deadline):
        payload = entry["payload"]
        live, thread = self.live(entry, deadline)
        marker = delivery_marker(entry["id"])
        matches = [
            c
            for c in thread
            if c["kind"] == "comment" and c["login"] == payload["account"] and marker in c["body"]
        ]
        if matches:
            body = matches[0]["body"]
            exact = sha(body.encode()) == payload["body_digest"]
            if body.startswith(POST.DEPRECATED_PREFIX):
                exact = body == POST.deprecate_body(payload["body"], matches[0]["at"])
            if len(matches) == 1 and exact:
                return self.receipt(entry, matches[0]["id"], matches[0].get("url"))
            entry.update(
                state="uncertain",
                failures=5,
                error="delivery marker duplicated or body changed; inspect thread",
            )
            atomic(self.store.root / "outbox" / (entry["id"] + ".json"), entry)
            return None
        now = self.now()
        path = self.store.root / "outbox" / (entry["id"] + ".json")
        if now < entry["sent_at"] + 120:
            entry["next_attempt"] = entry["sent_at"] + 120
            atomic(path, entry)
            return None
        if not entry.get("absent_at"):
            entry["absent_at"] = now
            entry["next_attempt"] = now + 60
            atomic(path, entry)
            return None
        if now - entry["absent_at"] < 60:
            return None
        if entry.get("superseded"):
            return dict(state="superseded")
        if live["head"]["sha"] != payload["observed_head"]:
            return dict(
                state="held", reason="head differs from frozen observed head; repair required"
            )
        self.pages.verify_comment(entry, deadline)
        # Persist a new grace window before retrying identical bytes/action.
        entry["sent_at"] = now
        entry.pop("absent_at", None)
        atomic(path, entry)
        return self.deliver(entry, deadline)

    def deprecate(self, entry, deadline):
        receipt = self.store.receipt_of(entry["dependencies"][0])
        if receipt["state"] != "posted":
            return dict(state="not_applicable")
        poster = receipt.get("account", self.config["comment_accounts"][0])
        self.check_account(poster, deadline)
        repo = entry["pr"][3:].split("#")[0]
        for old in receipt.get("predecessors", []):
            # GitHub lets only a comment's author edit it. Comments another of
            # our logins wrote (the old command posted as tridge) are left to
            # deprecate-legacy-comments.sh, which runs as that login.
            if old.get("login") and old["login"] != poster:
                continue
            # Targets and original bytes are frozen at first send, never newest-thread selection.
            self.gh.request(
                f"repos/{repo}/issues/comments/{old['id']}",
                method="PATCH",
                account="comment",
                payload={"body": POST.deprecate_body(old["body"], old["at"])},
                deadline=deadline,
            )
        return dict(state="deprecated", comment_id=receipt["comment_id"])


class Board:
    def __init__(self, github, config):
        self.gh, self.config = github, config

    def deliver(self, entry, deadline):
        board = module("project-sync")
        board.gh = lambda query, **variables: self.gh.graphql(query, deadline=deadline, **variables)
        project = self.config.get("project_id")
        if not project:
            raise OSError("no frozen project ID configured")
        repo, number = entry["pr"][3:].split("#")
        live = self.gh.request(f"repos/{repo}/pulls/{number}", account="project", deadline=deadline)
        if entry.get("node_id") and live["node_id"] != entry["node_id"]:
            raise OSError("board PR node changed")
        thread = self.gh.thread(repo, int(number), account="project", deadline=deadline)
        accounts = self.config["comment_accounts"]
        comments = sorted(
            (
                c
                for c in thread
                if c["kind"] == "comment"
                and c["login"] in accounts
                and POST.MARKER in c["body"]
                and not c["body"].startswith(POST.DEPRECATED_PREFIX)
            ),
            key=lambda c: (c["at"] or "", c["id"]),
            reverse=True,
        )
        comment = next((c for c in comments if board.V.of_comment(c["body"])[0]), None)
        if comment is None:
            raise OSError("unknown board verdict")
        verdict = board.V.of_comment(comment["body"])[0]
        fields, options, _ = board.ensure_field(project)
        author_field, _ = board.ensure_author_field(project)
        rows = board.project_items(project)
        row = next((r for r in rows.values() if r["content"] == live["node_id"]), None)
        item = (
            row["item"]
            if row
            else board.gh(board.ADD_ITEM, project=project, content=live["node_id"])[
                "addProjectV2ItemById"
            ]["item"]["id"]
        )
        board.gh(board.SET_FIELD, project=project, item=item, field=fields, option=options[verdict])
        board.gh(
            board.SET_TEXT,
            project=project,
            item=item,
            field=author_field,
            text=live["user"]["login"],
        )
        verified = next(
            (
                r
                for r in board.project_items(project).values()
                if r["item"] == item and r["content"] == live["node_id"]
            ),
            None,
        )
        if (
            not verified
            or verified["result"] != verdict
            or verified["author"] != live["user"]["login"]
        ):
            raise OSError("board acknowledgement mismatch")
        return dict(
            state="synced",
            delivery_id=entry["id"],
            node_id=live["node_id"],
            item=item,
            comment_id=comment["id"],
            fields=dict(result=verdict, author=live["user"]["login"]),
        )


class Delivery:
    def __init__(self, store, github, config):
        self.store, self.gh, self.config = store, github, config
        self.publication = Publication(store, config)
        self.posting = Posting(store, github, self.publication, config)
        self.board = Board(github, config)

    def selected(self, entry):
        config = entry.get("configuration")
        if config is None and isinstance(entry["generation"], int):
            bundle = bundle_at(self.store, entry["pr"], entry["generation"])
            config = bundle["inputs"].get("configuration") if bundle else None
        if not config or config == self.config:
            return self
        from review_github import GitHub

        github = GitHub(
            config.get("github_recordings"),
            config.get("github_mode", "live"),
            config.get("github_accounts"),
            writes=config.get("github_writes", False),
        )
        return Delivery(self.store, github, config)

    def credentials(self, entry, deadline):
        selected = self.selected(entry)
        if selected is not self:
            return selected.credentials(entry, deadline)
        stack = ExitStack()
        roles = (
            ["project"]
            if entry["kind"] == "board"
            else ["comment"]
            if entry["kind"] in ("comment", "note", "deprecate")
            else []
        )
        keys = {
            self.config.get("github_accounts", {}).get(role, {}).get("id", role) for role in roles
        }
        try:
            for key in sorted(("account:github/" + k for k in keys), key=region):
                lock = acquire(
                    self.store.locks, key, min(deadline, time.monotonic() + 5), shared=True
                )
                if lock is None:
                    raise TimeoutError("GitHub credential lease busy")
                stack.enter_context(lock)
            return stack
        except BaseException:
            stack.close()
            raise

    def prepare(self, entry, deadline):
        selected = self.selected(entry)
        if selected is not self:
            return selected.prepare(entry, deadline)
        if entry["kind"] in ("comment", "note"):
            return self.posting.prepare(entry, deadline)
        if entry["kind"] in ("board", "deprecate"):
            dependencies = [
                self.store.receipt_of(ident, {})
                for ident in entry.get("dependencies", [])
            ]
            if any(
                receipt.get("state") in ("held", "not_applicable", "superseded")
                for receipt in dependencies
            ):
                return dict(state="not_applicable")
        if entry["kind"] == "board" and entry.get("outcome") == "not_applicable":
            return dict(state="not_applicable")
        return None

    def deliver(self, entry, deadline):
        selected = self.selected(entry)
        if selected is not self:
            return selected.deliver(entry, deadline)
        if self.config.get("frozen_inputs"):
            # a quality pilot's work: never published, posted or synced,
            # whichever process is draining it
            raise OSError("frozen inputs: no outward delivery")
        if entry["kind"] in ("publish", "annotation"):
            return self.publication.deliver(entry, deadline)
        if entry["kind"] in ("comment", "note"):
            return self.posting.deliver(entry, deadline)
        if entry["kind"] == "deprecate":
            return self.posting.deprecate(entry, deadline)
        if entry["kind"] == "board":
            return self.board.deliver(entry, deadline)
        raise OSError("unknown delivery kind")

    def destination_of(self, entry):
        selected = self.selected(entry)
        if selected is not self:
            return getattr(selected, "destination_of", lambda e: None)(entry)
        return self.publication.destination(entry["target"]) if entry["kind"] == "publish" else None

    def confirmed(self, entry):
        selected = self.selected(entry)
        if selected is not self:
            return getattr(selected, "confirmed", lambda e: None)(entry)
        return self.publication.confirmed(entry) if entry["kind"] == "publish" else None

    def current(self, entry):
        selected = self.selected(entry)
        if selected is not self:
            return getattr(selected, "current", lambda e: None)(entry)
        return self.publication.current(entry) if entry["kind"] == "publish" else None

    def reconcile(self, entry, deadline):
        selected = self.selected(entry)
        if selected is not self:
            return selected.reconcile(entry, deadline)
        if self.config.get("frozen_inputs"):
            raise OSError("frozen inputs: no outward delivery")
        if entry["kind"] in ("comment", "note"):
            return self.posting.reconcile(entry, deadline)
        return self.deliver(entry, deadline)

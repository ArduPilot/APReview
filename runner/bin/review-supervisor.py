#!/usr/bin/env python3
"""A finite candidate snapshot and durable claims drive four independent tracks."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import re
import datetime
from pathlib import Path
import sys
import subprocess
import time
import uuid

from review_guardian import alive, cleanup_attempt, identity, launch
from review_lock import account_slot, acquire, canonical, permit, try_lock
from review_schema import FILES, read_result
from review_store import Store, StubAdapter, atomic, delivery_id, digest, mkdir, read
import review_metrics
from review_discovery import Discovery, LABELS
from review_github import GitHub, RateLimited
from review_delivery import Delivery
from review_inference import prepare as prepare_inference

KINDS = ("primary", "cold", "validation", "reconciliation")
REFUSAL = "flagged for possible cybersecurity risk"


def refused(path):
    """Whether a Codex payload ended on OpenAI's content filter."""
    log = Path(path) / "payload.log"
    try:
        with open(log, "rb") as f:
            f.seek(max(0, log.stat().st_size - 65536))
            return REFUSAL.encode() in f.read()
    except OSError:
        return False


def scaled_wall(base, diff):
    """A pass's wall clock grows with the diff: a 30,000-line port cannot be
    read in the half hour a normal PR needs. Up to three times the base."""
    lines = (diff or "").count("\n")
    return base * min(3.0, max(1.0, lines / 10000.0))


DISCOVERY_RETRY = 60

# attempt errors that mean the payload never started
STARVED = ("account deadline", "permit deadline")

WALL = {"primary": 5400, "cold": 1800, "validation": 1800, "reconciliation": 2700}


def refresh(candidate):
    """The slice's read-only adapter uses supplied live metadata."""
    if candidate.get("refresh_error"):
        raise OSError("candidate refresh failed")
    return dict(candidate, **candidate.get("live", {}))


def reconciliation_snapshot(candidate):
    if candidate.get("reconciliation_error"):
        raise OSError("reconciliation snapshot failed")
    return {
        "title": candidate.get("title", ""),
        "head": candidate["head"],
        "thread": candidate.get("thread", []),
    }


class Supervisor:
    def __init__(
        self,
        data,
        directory,
        candidates=None,
        admission=14400,
        wall=None,
        request=None,
        mode="candidates",
        pool_size=8,
        permit_timeout=120,
        configuration=None,
    ):
        self.configuration = dict(configuration or {})
        if self.configuration:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo("Australia/Canberra"))
            import repos
            from review_inference import COMMANDS, PROMPTS

            self.configuration.setdefault("repos", repos.load())
            self.configuration.setdefault(
                "prompts",
                {
                    kind: (COMMANDS / ("review-" + name + ".md")).read_text()
                    for kind, name in PROMPTS.items()
                },
            )
            self.configuration.setdefault("date", now.date().isoformat())
            self.configuration.setdefault("stamp", now.strftime("%Y-%m-%d_%H-%M-%S"))
        self.last_save = 0
        self.last_summary = 0
        self.saved_state = None
        self.store = Store(Path(data).resolve())
        self.directory = Path(directory).resolve()
        mkdir(self.directory)
        self.run_id = str(self.directory)
        review_metrics.context(process="controller", run=self.directory.name, data=str(self.store.root))
        self.last_metrics = time.monotonic()
        self.lock = None
        self.owned = {}
        self.children = []
        self.next_claim = {}
        self.backoff = {}
        self.config = read(self.directory / "run.json")
        self.initial = (candidates, self.configuration.get("admission", admission), wall, request, mode,
                        self.configuration.get("pool_size", pool_size),
                        self.configuration.get("permit_timeout", permit_timeout))
        self.states = read(self.directory / "state.json", {})
        self.adapter = None
        self.discovery = None

    def initialize(self):
        if self.config is not None:
            if self.config.get("schema") != 1 or self.config.get("data") != str(self.store.root):
                raise ValueError("resume configuration uses a different store or schema")
            return
        candidates, admission, wall, request, mode, pool, permit_timeout = self.initial
        if candidates is None:
            candidates = []
        if pool < 2 or pool > 256:
            raise ValueError("reserved finishing slots require a pool of 2..256")
        unique = {}
        for candidate in candidates:
            candidate = dict(candidate)
            pr = canonical(
                candidate.get("pr", "pr:%s#%s" % (candidate["repository"], candidate["number"]))
            )
            candidate["pr"] = pr
            candidate["repository"] = pr[3:].split("#")[0]
            candidate["number"] = int(pr.split("#")[1])
            for field in ("head", "base", "merge_base", "node_id", "created_at"):
                if field not in candidate:
                    raise ValueError("candidate missing " + field)
            if pr in unique and unique[pr] != candidate:
                raise ValueError("conflicting duplicate candidate")
            unique[pr] = candidate
        ordered = sorted(unique.values(), key=lambda c: (c["created_at"], c["pr"]))
        self.config = {
            "schema": 1,
            "run": self.run_id,
            "data": str(self.store.root),
            "request": request or uuid.uuid4().hex,
            "mode": mode,
            "created": time.time(),
            "admission_deadline": time.time() + admission,
            "pool_size": pool,
            "permit_timeout": permit_timeout,
            "wall": wall,
            "wall_timeouts": dict(self.configuration.get("wall_timeouts", WALL)),
            "candidates": ordered,
            "stub": os.environ.get("REVIEW_AI_STUB") == "1",
            "configuration": self.configuration,
            "phases": (
                {"initial": {"state": "admitted", "snapshots": {mode: ordered}}}
                if self.initial[0] is not None
                else {}
            ),
            "plain_guardians": os.environ.get("REVIEW_GUARDIAN_PLAIN") == "1",
            "observation": self.store.ticket(),
        }
        atomic(self.directory / "run.json", self.config)

    def phase_name(self, phase):
        return phase + (
            ":followup"
            if self.config.get("mode") == "all" and "followup" in self.config.get("phases", {})
            else ""
        )

    def project(self, candidate, phase, generation=None, quiet=False):
        root = self.config["configuration"].get("routing_root")
        pause = None
        if root and candidate["pr"] not in self.owned:
            # A controller can appear after a handoff's finite process scan.
            # Its queued/discovery projections are admission too: hold a short
            # shared fence while checking ownership and creating their intents.
            pause = try_lock(self.store.locks, "pause", shared=True)
            if pause is None:
                return
        try:
            if root:
                from review_routing import load, owner
                if owner(load(root), candidate.get("mode", self.config["mode"]), candidate["repository"]) != "new":
                    return
            self._project(candidate, phase, generation, quiet)
        finally:
            if pause:
                pause.close()

    def _project(self, candidate, phase, generation=None, quiet=False):
        pr = candidate["pr"]
        patch = {
            "ticket": candidate.get("observation", self.config["observation"]),
            "ci": candidate.get("ci"),
            "progress": phase,
            # no candidate here: it carries the whole diff and thread, nothing
            # reads it from a page's rows, and every projection rewrote it
            "removed": candidate.get("classification") == "DROPPED",
        }
        if generation is not None:
            patch["generation"] = generation
        intents = []
        if phase != "discovery":
            phase += "-" + str(patch["ticket"]) + "-" + str(generation or 0)
        operation = digest([self.run_id, self.phase_name(phase), pr])
        for target in dict.fromkeys(candidate.get("destinations", [])):
            target = canonical(target)
            intents.append(
                {
                    "kind": "projection",
                    "target": target,
                    "gate": "page",
                    "patches": {
                        pr: dict(
                            patch,
                            removed=candidate.get("membership_removed", {}).get(
                                target, patch["removed"]
                            ),
                        )
                    },
                    "configuration": self.config["configuration"],
                }
            )
            intents.append(
                {
                    "kind": "publish",
                    "target": target,
                    "gate": "page",
                    "configuration": self.config["configuration"],
                    "dependencies": [delivery_id(pr, operation, "projection", target)],
                }
            )
        journalled = (self.store.root / "operations"
                      / (digest([self.run_id, self.phase_name(phase), canonical(pr)]) + ".json"))
        if journalled.exists():
            # Already journalled, perhaps with some destinations left out:
            # never rebuilt, since recovery fans out what is stored.
            return
        if intents:
            # A pure re-observation (discovery, reuse) whose page, as last
            # verified uploaded, already shows exactly what it would show now
            # is merged directly, its ticket still fencing older queued
            # observations, and journals nothing. Anything else journals a
            # projection and publish as before.
            kept = []
            for projection, publish in zip(intents[::2], intents[1::2]):
                if not self.examine(projection["target"], pr, projection["patches"][pr], quiet):
                    kept += [projection, publish]
            intents = kept
        if intents:
            intents.extend(self.landing_intents(pr, operation, intents, gate="page"))
            self.store.journal(self.run_id, self.phase_name(phase), pr, intents)

    def destination(self, target):
        """Where a page is published under this run's frozen configuration,
        in the form delivery records (Publication.destination)."""
        from urllib.parse import quote
        endpoint, path = canonical(target)[5:].split("/", 1)
        configured = self.config["configuration"].get("endpoints", {}).get(endpoint, {})
        return [configured.get("publish") or os.environ.get("REVIEW_PUBLISH"),
                (configured.get("url") or "").rstrip("/") + "/" + quote(path, safe="/")]

    def view(self, row, pr):
        """What a page would show for this row now, by the renderer's own model."""
        from review_render import bundle_at, comment_receipts, row_view
        bundle = bundle_at(self.store, pr, row["generation"]) if row and row.get("generation") is not None else None
        return row_view(row, bundle, self.store.claim(pr), comment_receipts(self.store, bundle),
                        read(self.store.root / "legacy-facts.json", {}) if bundle and bundle.get("legacy") else None)

    def examine(self, target, pr, patch, quiet):
        """True when a quiet observation was merged directly: the page's last
        verified upload already shows exactly what it would show now, at this
        destination. Recorded by delivery from the render it uploaded, never
        from intent, so a delayed or failed publish cannot make it match.
        The rows compared are exactly the rows written."""
        if not quiet:
            return False
        try:
            lock = try_lock(self.store.locks, target)
        except RuntimeError:
            return False
        if lock is None:
            return False
        try:
            with lock:
                rows = self.store._merge_membership(lock, target, {pr: patch}, write=False)
                published = rows.get(pr, {}).get("published") or {}
                epoch = read(self.store.root / "pages" / digest(canonical(target)) / "epoch.json", 0)
                stub = self.config.get("stub")
                # still the page's latest upload attempt, to this destination
                # (the stub adapter records neither epoch nor destination)
                current = (published.get("epoch") == epoch or (stub and published.get("epoch") is None)) and (
                    published.get("destination") == self.destination(target)
                    or (stub and published.get("destination") is None))
                if published and current and published.get("view") == self.view(rows.get(pr), pr):
                    self.store.write_membership(lock, target, rows)
                    review_metrics.count("local", "projection merged unchanged")
                    return True
                review_metrics.count("local", "projection journalled")
                return False
        except (ValueError, OSError):
            return False

    def generation_receipts(self, pr, generation):
        """The receipts of one generation's publishes, comment and board sync,
        read by their ids from its bundle; a receipt never changes once
        written, so one found is not read again."""
        if not isinstance(generation, int):
            return []
        cache = self.__dict__.setdefault("_generation_receipts", {})
        key = (pr, generation)
        if key not in cache:
            bundle = read(self.store.pr_dir(pr) / "generations" / str(generation) / "bundle.json")
            if bundle is None:
                return []           # claimed, not yet accepted: look again next save
            cache[key] = dict(ids=[i["id"] for i in bundle.get("intents", [])
                                   if i["kind"] in ("publish", "comment", "board")], found={})
        entry = cache[key]
        for ident in entry["ids"]:
            if ident not in entry["found"]:
                receipt = self.store.receipt_of(ident)
                if receipt:
                    entry["found"][ident] = receipt
        return list(entry["found"].values())

    def save(self, force=False):
        now = time.monotonic()
        if force or now - getattr(self, "last_metrics", now) > 60:
            review_metrics.flush()
            self.last_metrics = now
        if not force and now - self.last_save < 3:
            return
        # Only the receipts each PR's generation names: indexing every
        # receipt in the store cost a controller two minutes at start, and
        # grew with every delivery ever made.
        for pr, state in self.states.items():
            for receipt in self.generation_receipts(pr, state.get("generation")):
                if receipt["kind"] == "publish":
                    state["publish"][receipt["target"]] = receipt["state"]
                elif receipt["kind"] in ("comment", "board"):
                    state[receipt["kind"]] = receipt["state"]
        changed = digest(self.states) != self.saved_state
        if changed:
            atomic(self.directory / "state.json", self.states)
            self.saved_state = digest(self.states)
        if changed or force or now - self.last_summary >= 30:
            self.last_summary = now
            debts = [x for x in (read(p) for p in (self.store.root / "outbox").glob("*.json")) if x]
            atomic(
                self.directory / "summary.json",
                {
                    "schema": 1,
                    "run": self.run_id,
                    **identity(),
                    "state": "complete" if self.summary_finished() else "running",
                    "heartbeat": time.time(),
                    "prs": self.states,
                    "phases": self.config.get("phases", {}),
                    "delivery_deferred": [x["id"] for x in debts if x["pr"] in self.states],
                    "attempts": [
                        str(p.parent) for p in (self.directory / "attempts").glob("*/job.json")
                    ],
                },
            )
        self.last_save = now

    def summary_finished(self):
        phase_done = (
            self.config["mode"] != "all"
            or self.config.get("phases", {}).get("followup", {}).get("state") == "complete"
        )
        return not self.active() and phase_done

    def active(self):
        return any(
            s["review"] in ("pending", "claimed", "reviewing", "reconciling")
            for s in self.states.values()
        )

    def inputs(self, candidate):
        return {k: v for k, v in candidate.items() if k not in ("live", "stub")}

    def finish(self, pr, review, reason=None):
        state = self.states[pr]
        state["review"] = review
        candidate = state.get("candidate") or next(
            (c for c in self.config["candidates"] if c["pr"] == pr), None
        )
        # the claim first: the page rendered for this projection shows it
        lock = self.owned.get(pr)
        if lock and review == "deferred":
            claim = self.store.claim(pr)
            if claim and claim["run"] == self.run_id:
                claim["status"] = "deferred"
                self.store.save_claim(lock, pr, claim)
        if candidate:
            self.project(
                candidate,
                review,
                state.get("generation") if review in ("accepted", "reused") else None,
                # reuse and drops change no claim; deferral and acceptance do
                quiet=review in ("reused", "dropped"),
            )
        if reason:
            state["reason"] = reason
        # a deferral, with or without a claim, is work still owed: it keeps
        # the PR in the followup window until it is settled
        if review == "deferred":
            self.store.mark_pending(pr, reason or "deferred")
        elif review in ("accepted", "reused") or (review == "dropped" and reason != "label removed"):
            # a label run dropping a PR whose label went does not settle a
            # followup review it may still owe
            self.store.clear_pending(pr)
        lock = self.owned.pop(pr, None)
        if lock:
            lock.close()

    PREFETCH_AGE = 60
    ADMIT_BATCH = 16

    def prefetch(self, candidates):
        """Refresh the candidates about to be claimed in parallel. Each is a
        few GitHub round trips and a pass admits every pending PR; one at a
        time that took most of an hour."""
        if not self.discovery:
            return
        store = getattr(self, "prefetched", None)
        if store is None:
            store = self.prefetched = {}
        now = time.monotonic()
        todo = []
        for c in candidates:
            if now - store.get(c["pr"], (-1e9, None))[0] < self.PREFETCH_AGE:
                continue
            if self.settled_at_discovery(c):
                continue                # admission settles it from discovery's read
            # A PR another pass holds will not be claimed this pass; fetching
            # it every minute while it waits spent the hourly GitHub budget.
            try:
                probe = try_lock(self.store.locks, c["pr"])
            except (RuntimeError, OSError, ValueError):
                probe = None
            if probe is None:
                continue
            probe.close()
            todo.append(c)
        if not todo:
            return
        def one(candidate):
            # best effort: a failure here is repeated, and reported, by the
            # claim's own refresh
            try:
                return self.discovery.refresh(candidate)
            except Exception:
                return None
        workers = int(self.config["configuration"].get("discovery_workers", 8))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for candidate, result in zip(todo, pool.map(one, todo)):
                if result is not None:
                    store[candidate["pr"]] = (time.monotonic(), result)

    DISCOVERY_FRESH = 3600

    def too_soon(self, pr, candidate):
        """A PR reviewed less than rereview_hours ago waits, unless it was
        asked for by name or is on a dev call's page for today or tomorrow.
        Pushes drive re-reviews (all but one of 309 on blu6 were of a new
        head), and a review after the interval covers every push since."""
        hours = float(self.config["configuration"].get("rereview_hours", 0))
        # only modes whose PRs later runs reliably revisit: followup, and the
        # label sweeps; a review asked for by name or by author is done now
        if hours <= 0 or candidate.get("mode", self.config["mode"]) not in ("followup", *LABELS):
            return False
        current = self.store.current(pr)
        if not current:
            return False
        generation = self.store.pr_dir(pr) / "generations" / str(current["generation"])
        bundle = read(generation / "bundle.json", {})
        if bundle.get("legacy"):
            return False
        try:
            age = time.time() - generation.stat().st_mtime
        except OSError:
            return False
        return age < hours * 3600 and not self.call_soon(candidate)

    @staticmethod
    def call_soon(candidate):
        """On a DevCallEU or DevCallTopic page dated today or tomorrow."""
        # the time zone call pages are dated in
        from zoneinfo import ZoneInfo
        today = datetime.datetime.now(ZoneInfo("Australia/Canberra")).date()
        for target in candidate.get("destinations", []):
            m = re.search(r"/DevCallReviews/(\d{4})_(\d{2})_(\d{2})/(DevCallEU|DevCallTopic)/", target)
            try:
                if m and 0 <= (datetime.date(int(m[1]), int(m[2]), int(m[3])) - today).days <= 1:
                    return True
            except ValueError:
                return True             # a date it cannot read is not held back
        return False

    def settled_at_discovery(self, candidate):
        """Discovery found it unchanged or gone within the hour: admission
        settles it from that read instead of asking GitHub again. A PR going
        to review is always read again just before its first pass."""
        if candidate.get("reason") == "label removed":
            return False        # admission may find it under another label
        return (candidate.get("classification") in ("REUSE", "DROPPED")
                and time.time() - candidate.get("discovered_at", 0) < self.DISCOVERY_FRESH)

    def prefetched_refresh(self, pr):
        taken = getattr(self, "prefetched", {}).pop(pr, None)
        if not taken or time.monotonic() - taken[0] >= self.PREFETCH_AGE:
            return None
        return taken[1]

    def claim_candidate(self, candidate):
        routing_root = self.config["configuration"].get("routing_root")
        if routing_root:
            from review_routing import load, owner
            if owner(load(routing_root), candidate.get("mode", self.config["mode"]), candidate["repository"]) != "new":
                self.finish(candidate["pr"], "deferred", "ownership transferred")
                return
        pr = candidate["pr"]
        state = self.states[pr]
        if time.time() < self.next_claim.get(pr, 0):
            return
        lock = try_lock(self.store.locks, pr)
        if lock is None:
            delay = min(30, self.backoff.get(pr, 0) + 1)
            self.backoff[pr] = delay
            self.next_claim[pr] = time.time() + delay
            state["reason"] = "PR busy"
            return
        self.owned[pr] = lock
        if not self.store.clean_owner(lock):
            self.finish(pr, "deferred", "previous payload not empty")
            return
        self.store.recover_pr(lock, pr)
        current = self.store.bundle(pr)
        existing = self.store.claim(pr)
        if existing and existing["node_id"] != candidate["node_id"]:
            self.finish(pr, "deferred", "stored node id changed")
            return
        try:
            fresh = dict(candidate) if self.settled_at_discovery(candidate) else self.prefetched_refresh(pr)
            if fresh is None:
                fresh = self.discovery.refresh(candidate) if self.discovery else refresh(candidate)
        except RateLimited as error:
            # The allowance comes back within the hour: wait for it rather
            # than defer the PR, which would lose it for this run.
            self.owned.pop(pr).close()
            self.next_claim[pr] = max(time.time() + 60, (error.reset or 0) + 5)
            state["reason"] = "GitHub rate limit; retrying after the reset"
            return
        except OSError as error:
            self.finish(pr, "deferred", str(error))
            return
        if fresh.get("node_id") != candidate["node_id"]:
            self.finish(pr, "deferred", "node id changed")
            return
        fresh["destinations"] = list(
            dict.fromkeys(candidate.get("destinations", []) + fresh.get("destinations", []))
        )
        fresh["post"] = fresh.get("post", False) or candidate.get("post", False)
        if (
            fresh.get("classification") == "DROPPED"
            or fresh.get("open", True) is False
            or (fresh.get("draft", False) and "AIReview" not in fresh.get("labels", []))
        ):
            self.project(fresh, "refresh-" + str(fresh.get("observation", 0)))
            self.finish(pr, "dropped", fresh.get("reason"))
            return
        if fresh.get("classification") == "DEFERRED":
            self.finish(pr, "deferred", fresh.get("reason", "candidate deferred"))
            return
        same_request = (
            current
            and current["request"] == self.config["request"]
            and current["inputs"]["head"] == fresh["head"]
        )
        if current and (
            same_request
            or (
                fresh.get("mode", self.config["mode"]) != "pr"
                and current["inputs"]["head"] == fresh["head"]
            )
        ):
            state["generation"] = current["generation"]
            self.project(fresh, "reuse", current["generation"], quiet=True)
            self.finish(pr, "accepted" if same_request else "reused")
            return
        if fresh.get("classification") == "REUSE":
            self.project(fresh, "reuse", quiet=True)
            self.finish(pr, "reused", fresh.get("reason"))
            return
        claim = self.store.claim(pr)
        continuing = (claim and claim["run"] == self.run_id and claim["request"] == self.config["request"]
                      and claim["status"] == "active")
        if not continuing and self.too_soon(pr, fresh):
            # pushes come in bursts: one review after the interval covers them all
            self.finish(pr, "deferred", "re-review interval")
            return
        inputs = self.inputs(fresh)
        if (
            claim
            and claim["run"] == self.run_id
            and claim["request"] == self.config["request"]
            and claim["status"] == "active"
        ):
            # An admitted generation keeps its pinned inputs across controller death.
            inputs = claim["inputs"]
            fresh.update(inputs)
        if not (
            claim
            and claim["run"] == self.run_id
            and claim["request"] == self.config["request"]
            and claim["status"] == "active"
            and claim["inputs"] == inputs
        ):
            claim = self.store.allocate(lock, pr, self.run_id, self.config["request"], inputs)
        self.project(fresh, "refresh-" + str(fresh.get("observation", 0)))
        state.update(review="claimed", generation=claim["generation"], candidate=fresh, attempts={})
        if claim.get("carried"):
            # passes an earlier claim finished over the same review inputs
            state["carried"] = sorted(claim["carried"])
        for path in claim["attempts"]:
            job = read(Path(path) / "job.json")
            if job and job["input_digest"] == digest(inputs):
                state["attempts"].setdefault(job["kind"], []).append(path)

    def progress(self, pr):
        claim = self.store.claim(pr) or {}
        return len(claim.get("selected", {}))

    def account_free(self, kind):
        """Whether this kind's exclusive account lease is available now. A
        probe, not a reservation: the guardian still takes the lease itself."""
        provider = "claude" if kind in ("primary", "reconciliation") else "codex"
        config = getattr(self, "config", None)
        if config is None:
            return True
        if config["stub"]:
            key = "account:%s/stub" % provider
            cap = 1 if os.environ.get("REVIEW_STUB_EXCLUSIVE") == "1" else 8
        else:
            settings = config["configuration"].get("providers", {}).get(provider, {})
            if not settings.get("account"):
                return True
            key = "account:%s/%s" % (provider, settings["account"])
            cap = 1 if settings.get("exclusive_account") else settings.get("account_slots", 8)
        # The provider's permit pool bounds it too: primary and cold passes
        # leave slot 0 for the passes that finish a PR.
        pool = int(config.get("pool_size", 8)) if config else 8
        cap = min(cap, max(1, pool - 1) if kind in ("primary", "cold") else pool)
        # A slot probe is a point in time: one scheduling pass must not launch
        # more passes than there are slots, all to wait out the lease.
        live = 0
        for state in getattr(self, "states", {}).values():
            for other, paths in (state.get("attempts") or {}).items():
                mine = "claude" if other in ("primary", "reconciliation") else "codex"
                if mine == provider and paths:
                    status = read(Path(paths[-1]) / "status.json", {})
                    if status.get("state") != "terminal":
                        live += 1
        if live >= cap:
            return False
        try:
            probe = account_slot(self.store.locks, key, cap,
                                 skip_finishing_slot=kind in ("primary", "cold"))
        except RuntimeError:
            return True
        if probe is None:
            return False
        probe.close()
        # Another run's passes count too: probe the provider's permit pool,
        # or this run's passes lose every race and wait out the permit.
        try:
            permit_probe = permit(self.store.locks, provider, pool,
                                  skip_finishing_slot=kind in ("primary", "cold"))
        except (RuntimeError, OSError, ValueError):
            return True
        if permit_probe is None:
            return False
        permit_probe.close()
        return True

    def attempt_state(self, path):
        status = read(Path(path) / "status.json", {})
        if alive(status):
            return "live"
        if status.get("state") == "terminal" and status.get("empty"):
            return "done"
        job = read(Path(path) / "job.json")
        manager = read(Path(path) / "manager.json", {})
        if alive(manager) or time.time() - job["registered"] < 1:
            return "live"
        return "failed" if cleanup_attempt(Path(path), time.monotonic() + 5) else "blocked"

    def start_attempt(self, candidate, kind, claim):
        pr = candidate["pr"]
        state = self.states[pr]
        attempt_id = uuid.uuid4().hex
        path = self.directory / "attempts" / attempt_id
        mkdir(path)
        job = {
            **claim["inputs"],
            "schema": 1,
            "run": self.run_id,
            "job": pr + ":" + kind,
            "attempt": attempt_id,
            "generation": claim["generation"],
            "kind": kind,
            "pr": pr,
            "provider": "claude" if kind in ("primary", "reconciliation") else "codex",
            "account": "stub",
            "exclusive_account": os.environ.get("REVIEW_STUB_EXCLUSIVE") == "1",
            "account_slots": 8,
            "input_digest": digest(claim["inputs"]),
            "abort_path": str(self.directory / "abort.json"),
            "registered": time.time(),
            "env": {
                k: v
                for k, v in os.environ.items()
                if k.startswith("REVIEW_") or k in ("PATH", "HOME", "LANG", "PYTHONPATH")
            },
            "wall_timeout": self.config["wall"] or scaled_wall(
                self.config.get("wall_timeouts", WALL)[kind], claim["inputs"].get("diff")),
            "pool_size": self.config["pool_size"],
            "permit_timeout": self.config["permit_timeout"],
            "command": [sys.executable, str(Path(__file__).with_name("review_stub.py"))],
        }
        behavior = candidate.get("stub", {}).get(kind, {})
        if isinstance(behavior, list):
            index = len(state["attempts"].get(kind, []))
            behavior = behavior[min(index, len(behavior) - 1)] if behavior else {}
        job["stub"] = behavior
        if kind == "validation":
            result = read_result(
                Path(claim["selected"]["primary"]) / "review.json",
                read(Path(claim["selected"]["primary"]) / "job.json"),
            )
            job["primary_ids"] = [x["id"] for x in result["findings"]]
            job["primary_result"] = result
        if kind == "reconciliation":
            if self.discovery:
                repo, number = candidate["repository"], candidate["number"]
                live = self.discovery.gh.request(f"repos/{repo}/pulls/{number}")
                job["fresh_snapshot"] = dict(
                    title=live["title"],
                    head=live["head"]["sha"],
                    thread=self.discovery.gh.thread(repo, number),
                )
            else:
                job["fresh_snapshot"] = reconciliation_snapshot(refresh(candidate))
            job["finding_ids"] = []
            job["results"] = {}
            for previous in ("primary", "cold", "validation"):
                source = Path(claim["selected"][previous])
                result = read_result(source / FILES[previous], read(source / "job.json"))
                job["results"][previous] = result
                job["finding_ids"] += [
                    x["id"] for x in result.get("findings", result.get("new", []))
                ]
        job["previous_ids"] = [
            f["id"] for f in (candidate.get("previous_comment") or {}).get("findings", [])
        ]
        if kind == "reconciliation":
            job["finding_ids"] += job["previous_ids"]
        if kind in state.get("refused", []):
            job["refused_before"] = True
        if not self.config["stub"]:
            prepare_inference(self.store, path, job, self.config["configuration"])
        atomic(path / "job.json", job)
        claim["attempts"].append(str(path))
        self.store.save_claim(self.owned[pr], pr, claim)
        state["attempts"].setdefault(kind, []).append(str(path))
        self.save(force=True)
        child = launch(self.store.root, path, self.owned[pr])
        review_metrics.count("inference", "launched " + kind)
        if child:
            self.children.append(child)

    def advance(self, pr):
        state = self.states[pr]
        candidate = state["candidate"]
        claim = self.store.claim(pr)
        live = False
        failed = False
        failed_reason = "pass failed after retry"
        retryable = set()
        for kind, attempts in list(state["attempts"].items()):
            if kind in claim["selected"]:
                continue
            path = attempts[-1]
            outcome = self.attempt_state(path)
            if outcome == "live":
                live = True
                continue
            status = read(Path(path) / "status.json", {})
            if outcome == "done" and status.get("error") in STARVED and not status.get("aborted"):
                # The payload never ran: another PR held the account or the
                # permit for the whole wait. That is not a failed pass.
                state.setdefault("starved", []).append(attempts.pop())
                if not attempts:
                    del state["attempts"][kind]
                continue
            # A pass that states what it could not cover is accepted on its
            # last try: a PR too large to finish in one pass otherwise never
            # gets a review. Reconciliation must be complete.
            last_try = len(attempts) >= 2 and kind != "reconciliation"
            if (
                outcome == "done"
                and status.get("exit") == 0
                and not status.get("timed_out")
                and not status.get("aborted")
                and (status.get("result_status") == "complete"
                     or (last_try and status.get("result_status") == "incomplete"))
            ):
                try:
                    read_result(Path(path) / FILES[kind], read(Path(path) / "job.json"))
                except (ValueError, OSError, KeyError):
                    pass
                else:
                    claim["selected"][kind] = path
                    self.store.save_claim(self.owned[pr], pr, claim)
                    continue
            if refused(path):
                # OpenAI's content filter declined the prompt; the retry goes
                # to a different Codex model and effort
                state.setdefault("refused", [])
                if kind not in state["refused"]:
                    state["refused"].append(kind)
            if len(attempts) >= 2 or status.get("quota_blocked") or outcome == "blocked":
                failed = True
                if status.get("quota_blocked"):
                    failed_reason = "quota paused"
                elif outcome == "blocked":
                    failed_reason = "payload cleanup blocked"
            else:
                retryable.add(kind)
        abort = (self.directory / "abort.json").exists()
        if failed or abort:
            if not live:
                self.finish(pr, "deferred", "aborted" if abort else failed_reason)
            return
        if len(claim["selected"]) == 4:
            intents = self.intents(candidate, claim["generation"])
            pointer = self.store.accept(self.owned[pr], pr, claim, intents)
            state["publish"].update(
                {x["target"]: "owed" for x in intents if x["kind"] == "publish"}
            )
            state["generation"] = pointer["generation"]
            self.finish(pr, "accepted")
            return
        ready = ["primary", "cold"]
        if "primary" in claim["selected"]:
            ready.append("validation")
        if all(k in claim["selected"] for k in ("primary", "cold", "validation")):
            ready.append("reconciliation")
        for kind in ready:
            if kind in claim["selected"]:
                continue
            attempts = state["attempts"].get(kind, [])
            if attempts and kind not in retryable:
                continue
            if not self.account_free(kind):
                # launching now would only build a worktree to time out on
                # the lease; the next pass looks again
                continue
            try:
                self.start_attempt(candidate, kind, claim)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                # a guardian that cannot start (no user bus, systemd-run
                # refused) defers this PR; it must not end the run
                state["reason"] = str(error)
                if not any(
                    self.attempt_state(paths[-1]) == "live" for paths in state["attempts"].values()
                ):
                    self.finish(pr, "deferred", str(error))
                return
            state["review"] = "reconciling" if kind == "reconciliation" else "reviewing"

    def intents(self, candidate, generation):
        pr = candidate["pr"]
        targets = list(candidate.get("destinations", []))
        endpoint = (
            "stub"
            if self.config["stub"]
            else self.config["configuration"].get("endpoint", "review")
        )
        retained = "page:%s/%s/%s/%d/%d.html" % (
            endpoint,
            self.config["configuration"].get("retained_prefix", "PRReviews") if not self.config["stub"] else "PRReviews",
            candidate["repository"],
            candidate["number"],
            generation,
        )
        targets.append(retained)
        intents = [
            {"kind": "publish", "target": canonical(t), "retained": t == retained}
            for t in dict.fromkeys(targets)
        ]
        publication_ids = [delivery_id(pr, generation, "publish", x["target"]) for x in intents]
        comment = {
            "kind": "comment",
            "target": pr,
            "dependencies": publication_ids,
            "payload": {"report": retained},
            "outcome": "posted" if candidate.get("post", False) else "not_applicable",
        }
        intents.append(comment)
        if not self.config["stub"]:
            for target in dict.fromkeys(candidate.get("destinations", [])):
                intents.append(
                    {
                        "kind": "annotation",
                        "target": canonical(target),
                        "dependencies": [delivery_id(pr, generation, "comment", pr)],
                    }
                )
            intents.append(
                {
                    "kind": "deprecate",
                    "target": pr,
                    "dependencies": [delivery_id(pr, generation, "comment", pr)],
                }
            )
        intents.append(
            {
                "kind": "board",
                "target": self.config["configuration"].get("project_id", "stub-board"),
                "outcome": "synced" if candidate.get("post", False) else "not_applicable",
                "node_id": candidate["node_id"],
                "dependencies": [delivery_id(pr, generation, "comment", pr)],
            }
        )
        operation = digest([self.run_id, self.phase_name("discovery"), pr])
        # Discovery can skip journalling a PR (pause fence busy, PR not new-
        # owned then); a dependency on its projection would never settle and
        # held the PR's publishes and comment for good.
        # per intent: discovery may have journalled only some destinations
        stored = read(self.store.root / "operations" / (operation + ".json"), {})
        discovered = {i["id"] for i in stored.get("intents", [])}
        for target in candidate.get("destinations", []):
            target = canonical(target)
            dependency = delivery_id(pr, operation, "projection", target)
            intents.append(
                {
                    "kind": "projection",
                    "target": target,
                    "patches": {pr: {"generation": generation}},
                    "dependencies": [dependency] if dependency in discovered
                    or self.store.has_receipt(dependency)
                    or (self.store.root / "outbox" / (dependency + ".json")).exists() else [],
                }
            )
            for intent in intents:
                if intent["kind"] == "publish" and intent["target"] == target:
                    intent["dependencies"] = [delivery_id(pr, generation, "projection", target)]
        intents.extend(self.landing_intents(pr, generation, intents))
        return intents

    def landing_intents(self, pr, generation, intents, gate=None):
        import re

        if self.config["stub"]:
            return []
        destinations = {}
        for intent in intents:
            match = re.fullmatch(
                r"(page:[^/]+/DevCallReviews/\d{4}[-_]\d{2}[-_]\d{2})/[^/]+/devcall_pr_reviews.html",
                intent["target"],
            )
            if intent["kind"] == "publish" and match:
                target = match[1] + "/devcall_pr_reviews.html"
                destinations.setdefault(target, []).append(
                    delivery_id(pr, generation, "publish", intent["target"])
                )
        return [
            dict(
                kind="publish",
                target=target,
                landing=True,
                dependencies=dependencies,
                configuration=self.config["configuration"],
                **({"gate": gate} if gate else {}),
            )
            for target, dependencies in destinations.items()
        ]

    def setup_adapters(self):
        cfg = self.config["configuration"]
        github = GitHub(
            cfg.get("github_recordings"),
            cfg.get("github_mode", "live"),
            cfg.get("github_accounts"),
            writes=cfg.get("github_writes", False),
            http_cache=(self.store.root / "http-cache") if cfg.get("github_http") else None,
        )
        self.adapter = (
            StubAdapter(self.store.root)
            if self.config["stub"]
            else Delivery(self.store, github, cfg)
        )
        self.discovery = Discovery(github, cfg, self.store) if cfg else None

    def phase_summaries(self, phase):
        summaries = {}
        for mode, candidates in phase.get("snapshots", {}).items():
            states = {c["pr"]: phase.get("outcomes", {}).get(c["pr"], {}) for c in candidates}
            summaries[mode] = {
                "classification": {
                    name: sum(c.get("classification", "REVIEW") == name for c in candidates)
                    for name in ("REVIEW", "REUSE", "DROPPED", "DEFERRED")
                },
                "outcomes": states,
                "deferrals": {
                    pr: state.get("reason", "delivery deferred")
                    for pr, state in states.items()
                    if state.get("review") == "deferred"
                    or state.get("comment") in ("owed", "delivery_deferred")
                },
            }
        return summaries

    def discover_phase(self, phase):
        if phase in self.config["phases"]:
            return

        modes = (
            LABELS if phase == "labels" else [self.config["mode"] if phase == "initial" else phase]
        )
        def discover(mode):
            # a failed discovery (GitHub or the network down for a moment) is
            # tried twice more a minute apart, then that mode is left empty
            # for this run rather than ending it
            for attempt in range(3):
                try:
                    return self.discovery.discover(mode)
                except (OSError, TimeoutError) as error:
                    print("discovery of %s failed (%s): %s" % (mode, attempt + 1, str(error)[:200]),
                          file=sys.stderr, flush=True)
                    if attempt < 2:
                        time.sleep(DISCOVERY_RETRY)
            return []

        snapshots = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {mode: pool.submit(discover, mode) for mode in modes}
            for mode, future in futures.items():
                snapshots[mode] = future.result()
        merged = {}
        for mode, rows in snapshots.items():
            for row in rows:
                old = merged.get(row["pr"])
                row["selection_modes"] = [mode]
                if old:
                    destinations = list(dict.fromkeys(old["destinations"] + row["destinations"]))
                    post = old["post"] or row["post"]
                    selection_modes = list(dict.fromkeys(old["selection_modes"] + [mode]))
                    membership = {
                        **old.get("membership_removed", {}),
                        **row.get("membership_removed", {}),
                    }
                    if (
                        old["classification"] in ("DROPPED", "REUSE")
                        and row["classification"] == "REVIEW"
                    ):
                        merged[row["pr"]] = row
                    merged[row["pr"]].update(
                        destinations=destinations,
                        post=post,
                        selection_modes=selection_modes,
                        membership_removed=membership,
                    )
                else:
                    merged[row["pr"]] = row
        self.config["active_phase"] = phase
        self.config["phases"][phase] = {"snapshots": snapshots, "state": "admitted"}
        self.config["candidates"] = sorted(
            merged.values(), key=lambda row: (row["created_at"], row["pr"])
        )
        atomic(self.directory / "run.json", self.config)
        # only now, with the followup's candidates durable in this run, may
        # the window advance past what it looked at
        started = getattr(self.discovery, "followup_started", None)
        if isinstance(started, (int, float)) and started:
            atomic(self.store.root / "followup-coverage.json", {"at": started})
            self.discovery.followup_started = None
        self.admit_snapshot()

    def admit_snapshot(self):
        for candidate in self.config["candidates"]:
            self.project(candidate, "discovery", quiet=True)
            self.states[candidate["pr"]] = {
                "review": "pending",
                "phase": self.config.get("active_phase", "initial"),
                "publish": {},
                "comment": "owed",
                "board": "owed",
            }

    DRAIN_BUSY = 5

    def bounded_drain(self, startup=False):
        start = time.monotonic()
        budget = self.startup_budget if startup else 100
        seconds = max(0, self.startup_deadline - start) if startup else 60
        if not startup and any(x.get("review") in ("pending", "claimed", "reviewing", "reconciling")
                               for x in getattr(self, "states", {}).values()):
            # Deliveries run inside the scheduling loop. With PRs to admit or
            # advance, a minute of page publishing per pass held every launch
            # behind it (sixteen minutes before the first pass of one run);
            # the cron drain and the next pass carry the rest.
            seconds = min(seconds, self.DRAIN_BUSY)
        cursor = read(self.directory / "recovery.json")
        if startup and cursor is None:
            cursor = self.startup_work
        with review_metrics.scope("drain"):
            self._bounded_drain(start, budget, seconds, startup, cursor)

    def _bounded_drain(self, start, budget, seconds, startup, cursor):
        pending = self.store.recover(cursor, limit=min(50, budget), seconds=seconds)
        atomic(self.directory / "recovery.json", pending or None)
        if not getattr(self, "config", {}).get("configuration", {}).get("controller_delivers", True):
            # the drainer delivers; a controller only enqueues, so scheduling
            # never waits on GitHub or the publishing site
            return
        self.store.drain(
            self.adapter,
            limit=budget - self.store.last_recovered,
            seconds=max(0, seconds - (time.monotonic() - start)),
            snapshot=self.startup_delivery if startup else None,
        )

    def recover_controllers(self):
        deadline = self.startup_deadline
        pending = list(self.startup_runs)
        for _ in range(min(100, len(pending))):
            if time.monotonic() >= deadline:
                break
            directory = Path(pending.pop(0))
            self.startup_budget -= 1
            if directory == self.directory:
                continue
            summary = read(directory / "summary.json", {})
            if summary.get("state") == "complete":
                continue
            lock = try_lock(self.store.locks, "run:" + str(directory))
            if lock is None:
                continue
            lock.close()
            # The child arbitrates the final race with other coordinators.
            with open(directory / "recovery.log", "ab") as log:
                self.children.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--data",
                            str(self.store.root),
                            "--resume",
                            str(directory),
                            "--no-coordinator",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                )
        self.startup_runs = pending

    def run(self, coordinate=True):
        # Snapshot first; never acquire another controller's run while owning ours.
        self.startup_runs = sorted(
            str(p.parent) for p in (self.store.root / "runs").glob("*/run.json")
        )
        self.startup_work = self.store.snapshot()
        self.startup_delivery = self.store.delivery_snapshot()
        self.startup_budget = 100
        self.startup_deadline = time.monotonic() + 60
        if coordinate:
            self.recover_controllers()
        self.lock = try_lock(self.store.locks, "run:" + self.run_id)
        if self.lock is None:
            return 75
        # held shared for the controller's life: store GC deletes only while it
        # holds this exclusively, so no run starts, resumes or recovers then
        self.maintenance = acquire(self.store.locks, "maintenance", time.monotonic() + 900, shared=True)
        if self.maintenance is None:
            self.lock.close()
            return 75
        try:
            self.config = read(self.directory / "run.json")
            self.states = read(self.directory / "state.json", {})
            self.initialize()
            atomic(self.directory / "controller.json", dict(identity(), schema=1, run=self.run_id))
            atomic(self.directory / "summary.json", dict(
                identity(), schema=1, run=self.run_id, state="recovering", heartbeat=time.time(),
                prs=self.states, phases=self.config.get("phases", {}),
                delivery_deferred=[entry["id"] for entry in self.startup_delivery],
                attempts=[str(p.parent) for p in (self.directory / "attempts").glob("*/job.json")]))
            self.setup_adapters()
            os.environ["REVIEW_GUARDIAN_PLAIN"] = "1" if self.config["plain_guardians"] else "0"
            atomic(self.directory / "startup-runs.json", self.startup_runs)
            for candidate in self.config["candidates"]:
                self.project(candidate, "discovery", quiet=True)
                self.states.setdefault(
                    candidate["pr"],
                    {"review": "pending", "publish": {}, "comment": "owed", "board": "owed"},
                )
                state = self.states[candidate["pr"]]
                phase = self.config.get("active_phase", "initial")
                if state.get("phase", "initial") != phase:
                    state.update(review="pending", phase=phase)
                if state["review"] in ("claimed", "reviewing", "reconciling"):
                    state["review"] = "pending"
            self.bounded_drain(startup=True)
            if self.discovery and not self.config["phases"]:
                self.discover_phase("labels" if self.config["mode"] == "all" else "initial")
            next_drain = time.monotonic() + 1
            while True:
                if not self.active():
                    self.bounded_drain()
                    self.save(force=True)
                    if (
                        self.discovery
                        and self.config["mode"] == "all"
                        and "followup" not in self.config["phases"]
                    ):
                        phase = self.config["phases"]["labels"]
                        phase.update(state="complete", outcomes=dict(self.states))
                        for state in phase["outcomes"].values():
                            if state["comment"] == "owed":
                                state["comment"] = "delivery_deferred"
                        phase["summaries"] = self.phase_summaries(phase)
                        self.discover_phase("followup")
                        continue
                    for phase in self.config["phases"].values():
                        if phase["state"] != "complete":
                            phase.update(state="complete", outcomes=dict(self.states))
                        phase["summaries"] = self.phase_summaries(phase)
                    atomic(self.directory / "run.json", self.config)
                    break
                for child in self.children:
                    child.poll()
                pause = try_lock(self.store.locks, "pause", shared=True)
                paused = pause is None
                if pause:
                    pause.close()
                loop_start = time.monotonic()
                # Admit a batch per pass. Refreshing every pending PR in one
                # pass took ten minutes for 141 PRs, during which nothing
                # launched, and the first prefetches had expired before their
                # claims came round, so each was fetched twice.
                batch = set()
                if not paused and not (self.directory / "abort.json").exists():
                    eligible = [
                        c for c in self.config["candidates"]
                        if self.states[c["pr"]]["review"] == "pending"
                        and time.time() >= self.next_claim.get(c["pr"], 0)
                        and not (self.config["configuration"].get("quota", {}).get("paused"))
                        and time.time() < self.config["admission_deadline"]
                    ][:self.ADMIT_BATCH]
                    batch = {c["pr"] for c in eligible}
                    self.prefetch(eligible)
                for candidate in self.config["candidates"]:
                    pr = candidate["pr"]
                    state = self.states[pr]
                    if state["review"] == "pending":
                        old_claim = self.store.claim(pr)
                        continuing = (
                            old_claim
                            and old_claim["run"] == self.run_id
                            and old_claim["status"] == "active"
                        )
                        if (self.directory / "abort.json").exists():
                            if continuing:
                                self.claim_candidate(candidate)
                            else:
                                self.finish(pr, "deferred", "aborted")
                        elif self.config["configuration"].get("quota", {}).get("paused") and not continuing:
                            self.finish(pr, "deferred", "quota paused")
                        elif time.time() >= self.config["admission_deadline"] and not continuing:
                            self.finish(
                                pr,
                                "deferred",
                                "paused" if paused else state.get("reason", "admission deadline"),
                            )
                        elif continuing or (not paused and (pr in batch or not self.discovery)):
                            self.claim_candidate(candidate)
                admitted = time.monotonic()
                # Furthest along first: a PR ready to finish takes the next
                # free slot before a newly claimed one starts its first pass.
                for pr in sorted(self.owned, key=self.progress, reverse=True):
                    if pr in self.owned:
                        self.advance(pr)
                advanced = time.monotonic()
                if time.monotonic() >= next_drain:
                    self.bounded_drain()
                    next_drain = time.monotonic() + 1
                drained = time.monotonic()
                if drained - loop_start > 20:
                    # a slow pass of the loop delays every launch behind it;
                    # say which step took the time
                    print("loop %.0fs: admission %.0fs (%d pending), advance %.0fs (%d owned), drain %.0fs"
                          % (drained - loop_start, admitted - loop_start,
                             sum(1 for x in self.states.values() if x.get("review") == "pending"),
                             advanced - admitted, len(self.owned), drained - advanced),
                          file=sys.stderr, flush=True)
                self.save()
                time.sleep(0.05)
            self.save(force=True)
            return 0
        finally:
            for lock in self.owned.values():
                lock.close()
            self.owned.clear()
            self.maintenance.close()
            self.lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", default="candidates")
    parser.add_argument("--data", type=Path, default=os.environ.get("REVIEW_DATA"))
    parser.add_argument("--run", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--admission", type=float, default=14400)
    parser.add_argument("--wall", type=float)
    parser.add_argument("--request")
    parser.add_argument("--pool-size", type=int, default=4)
    parser.add_argument("--permit-timeout", type=float, default=120)
    parser.add_argument("--no-coordinator", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.data or not (args.resume or (args.run and (args.candidates or args.config))):
        parser.error("--data and either --resume or --run with --candidates required")
    return Supervisor(
        args.data,
        args.resume or args.run,
        read(args.candidates) if args.candidates else None,
        admission=args.admission,
        wall=args.wall,
        request=args.request,
        mode=args.mode,
        pool_size=args.pool_size,
        permit_timeout=args.permit_timeout,
        configuration=read(args.config) if args.config else None,
    ).run(coordinate=not args.no_coordinator)


if __name__ == "__main__":
    raise SystemExit(main())

"""Promotion is the commit point; indices can always be rebuilt from its chain."""
import hashlib
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sys
import time

import review_metrics
import uuid

from review_lock import acquire, canonical, region, try_lock
from review_schema import FILES, IDENTITY, evidence_paths, read_result


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdir(path):
    path = Path(path)
    if not path.exists():
        mkdir(path.parent)
        try:
            path.mkdir()
        except FileExistsError:
            pass
        fsync_dir(path.parent)


def atomic(path, value, crash=lambda point: None, prefix="record", *, encode=encoded):
    path = Path(path)
    mkdir(path.parent)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    with open(temp, "xb") as stream:
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())
    crash(prefix + "_file")
    os.replace(temp, path)
    crash(prefix + "_rename")
    fsync_dir(path.parent)
    crash(prefix + "_fsync")


def create_once(path, value):
    """Write path only if it does not exist yet, never replacing anything."""
    path = Path(path)
    mkdir(path.parent)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    with open(temp, "xb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temp, path)
        fsync_dir(path.parent)
        return True
    except FileExistsError:
        return False
    finally:
        os.unlink(temp)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_bytes())
    except FileNotFoundError:
        return default


def unlink(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return
    fsync_dir(Path(path).parent)


# A review pass may be accepted incomplete, gaps named, on its last try (the
# supervisor decides that); a reconciliation never.
ALLOWED_RESULT = {"primary": ("complete", "incomplete"), "cold": ("complete", "incomplete"),
                  "validation": ("complete", "incomplete"), "reconciliation": ("complete",)}


# What a review pass actually read. Two claims agreeing on these would give
# a pass the same work, so a pass one completed can serve the other.
REVIEW_FIELDS = ("repository", "number", "node_id", "head", "base", "merge_base", "diff", "rules", "title")
# Passes that can carry to a later claim. Reconciliation re-reads the live PR,
# so it is always run by the claim that accepts.
CARRIED = ("primary", "cold", "validation")


def review_key(inputs):
    previous = inputs.get("previous_comment") or {}
    return digest([[inputs.get(k) for k in REVIEW_FIELDS], digest(inputs.get("thread")),
                   previous.get("id"), previous.get("told_head")])


def delivery_id(pr, generation, kind, target):
    if target.startswith(("page:", "pr:", "account:")):
        target = canonical(target)
    return digest(["delivery-v1", canonical(pr), generation, kind, target])


def bundle_digest(path):
    def file_digest(path):
        with open(path, "rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    return digest([[str(p.relative_to(path)), file_digest(p)]
                   for p in sorted(Path(path).rglob("*")) if p.is_file()])


class Store:
    def __init__(self, root, crash=lambda point: None):
        self.root = Path(root)
        self.crash = crash
        for name in ("results", "outbox", "receipts", "membership", "operations", "owners", "runs"):
            mkdir(self.root / name)
        self.locks = self.root / "locks"

    def pr_dir(self, pr):
        repo, number = canonical(pr)[3:].split("#")
        return self.root / "results" / repo / number

    def gate(self, lock, pr):
        if lock is None or lock.closed or lock.shared or lock.key != canonical(pr) or Path(lock.path) != self.locks:
            raise RuntimeError("PR ownership required")

    def current(self, pr):
        return read(self.pr_dir(pr) / "current")

    def bundle(self, pr, pointer=None):
        pointer = pointer or self.current(pr)
        if not pointer:
            return None
        path = self.pr_dir(pr) / "generations" / str(pointer["generation"])
        # A promoted generation never changes; hashing all its evidence on
        # every read of every bundle, every pass, was most of a recovery
        # slice. Verified once per process, keyed by the digest it matched.
        verified = self.__dict__.setdefault("_verified", set())
        if (str(path), pointer["digest"]) not in verified:
            if bundle_digest(path) != pointer["digest"]:
                raise ValueError("bundle digest mismatch")
            verified.add((str(path), pointer["digest"]))
        return read(path / "bundle.json")

    def chain(self, pr):
        pointer = self.current(pr)
        last = float("inf")
        while pointer:
            if pointer["generation"] >= last:
                raise ValueError("invalid predecessor chain")
            last = pointer["generation"]
            bundle = self.bundle(pr, pointer)
            if bundle is None or bundle["generation"] != last or bundle["pr"] != canonical(pr):
                raise ValueError("invalid bundle identity")
            yield bundle
            pointer = bundle["previous"]

    def owner_path(self, lock):
        return self.root / "owners" / (str(lock.region) + ".json")

    def clean_owner(self, lock, deadline=None):
        from review_guardian import cleanup_attempt
        owner = read(self.owner_path(lock), {})
        registry = read(owner.get("registry", "/nonexistent"), {})
        attempts = list(owner.get("attempts", [])) + registry.get("attempts", [])
        for path in dict.fromkeys(attempts):
            limit = min(deadline, time.monotonic() + 5) if deadline is not None else time.monotonic() + 5
            if time.monotonic() >= limit or not cleanup_attempt(Path(path), limit):
                return False
        return True

    def allocate(self, lock, pr, run, request, inputs):
        self.gate(lock, pr)
        if not self.clean_owner(lock):
            raise RuntimeError("previous payload not empty")
        path = self.pr_dir(pr) / "claim.json"
        old = read(path, {"counter": 0})
        if old.get("node_id") not in (None, inputs["node_id"]):
            raise ValueError("canonical repository node id changed")
        claim = {"counter": old["counter"] + 1, "generation": old["counter"] + 1,
                 "run": run, "request": request, "inputs": inputs, "node_id": inputs["node_id"],
                 "status": "active", "attempts": [], "selected": {}}
        # A PR deferred part way (a pass that failed twice, the admission
        # deadline, an abort) keeps what it had done: a later claim over the
        # same review inputs takes the earlier claim's good passes rather than
        # paying for them again.
        if old.get("inputs") and review_key(old["inputs"]) == review_key(inputs):
            carried = {}
            for kind in CARRIED:
                earlier = old.get("selected", {}).get(kind)
                if kind == "validation" and "primary" not in carried:
                    break   # validation was made against that primary
                if earlier and self.carryable(earlier, kind, inputs):
                    carried[kind] = earlier
            if carried:
                claim["selected"] = dict(carried)
                claim["attempts"] = list(carried.values())
                claim["carried"] = dict(carried)
        atomic(path, claim)
        atomic(self.owner_path(lock), {"registry": str(path), "pr": canonical(pr)})
        self.crash("allocated")
        return claim

    def carryable(self, path, kind, inputs):
        """A finished, clean pass of this kind whose job read these inputs."""
        job = read(Path(path) / "job.json")
        status = read(Path(path) / "status.json", {})
        if not job or status.get("state") != "terminal" or status.get("exit") != 0 or \
                status.get("timed_out") or status.get("aborted") or not status.get("empty") or \
                status.get("result_status") not in ALLOWED_RESULT[kind] or job.get("kind") != kind or \
                any(status.get(k) != job.get(k) for k in IDENTITY) or review_key(job) != review_key(inputs):
            return False
        try:
            return read_result(Path(path) / FILES[kind], job)["status"] in ALLOWED_RESULT[kind]
        except (ValueError, OSError, KeyError):
            return False

    def last_worked(self, pr):
        """When we last worked on a PR, from local state alone: its newest
        accepted generation, or the posting of that generation's comment if
        later. None if we never reviewed it."""
        generations = [p for p in (self.pr_dir(pr) / "generations").glob("*") if p.name.isdigit()]
        if not generations:
            return None
        newest = max(generations, key=lambda p: int(p.name))
        times = [newest.stat().st_mtime]
        # when the comment was posted, from its file or the ledger, which
        # keeps the time a compacted receipt was written
        posted = self.receipt_time(delivery_id(pr, int(newest.name), "comment", pr))
        if posted:
            times.append(posted)
        return max(times)

    def mark_pending(self, pr, reason):
        """A review deferred, perhaps before any claim was made: keeps the PR
        in the followup window until it is reviewed, reused or dropped."""
        atomic(self.pr_dir(pr) / "pending.json", dict(at=time.time(), reason=reason))

    def clear_pending(self, pr):
        unlink(self.pr_dir(pr) / "pending.json")

    def in_followup_window(self, pr, cutoff):
        """False only when we know a PR is done with and old: its last work
        is before cutoff, no newer claim is still unfinished, and no comment
        is held for a human (who may post it at any time). A PR with no
        local record of our work stays in: a posting we cannot see locally
        must not take it out."""
        worked = self.last_worked(pr)
        if worked is None or worked >= cutoff or (self.pr_dir(pr) / "pending.json").exists():
            return True
        claim, current = self.claim(pr), self.current(pr)
        if claim and (not current or current["generation"] < claim["generation"]):
            return True
        receipt = self.receipt_of(delivery_id(pr, current["generation"], "comment", pr), {}) if current else {}
        return receipt.get("state") == "held"

    def claim(self, pr):
        return read(self.pr_dir(pr) / "claim.json")

    def save_claim(self, lock, pr, claim):
        self.gate(lock, pr)
        old = self.claim(pr)
        if old["generation"] != claim["generation"] or old["run"] != claim["run"]:
            raise ValueError("fenced claim")
        atomic(self.pr_dir(pr) / "claim.json", claim)

    def selected(self, pr, claim):
        results = {}
        jobs = {}
        if set(claim["selected"]) != set(FILES):
            raise ValueError("all passes required")
        for kind, path in claim["selected"].items():
            if path not in claim["attempts"]:
                raise ValueError("unregistered attempt")
            job = read(Path(path) / "job.json")
            status = read(Path(path) / "status.json", {})
            carried = claim.get("carried", {}).get(kind) == path and kind in CARRIED
            if carried:
                # an earlier claim's pass: same review inputs, not same claim
                if not self.carryable(path, kind, claim["inputs"]):
                    raise ValueError("carried attempt no longer matches")
            elif (status.get("state") != "terminal" or status.get("exit") != 0 or
                    status.get("timed_out") or status.get("aborted") or
                    status.get("result_status") not in ALLOWED_RESULT[kind] or not status.get("empty") or
                    any(status.get(k) != job[k] for k in IDENTITY) or
                    job["generation"] != claim["generation"] or job["run"] != claim["run"] or
                    job["kind"] != kind or job["input_digest"] != digest(claim["inputs"])):
                raise ValueError("unsuccessful or fenced attempt")
            result = read_result(Path(path) / FILES[kind], job)
            if result["status"] not in ALLOWED_RESULT[kind]:
                raise ValueError("incomplete result")
            results[kind] = result
            jobs[kind] = job
        if jobs["validation"].get("primary_result") != results["primary"]:
            raise ValueError("validation input does not match selected primary")
        if jobs["reconciliation"].get("results") != {k: results[k] for k in ("primary", "cold", "validation")}:
            raise ValueError("reconciliation inputs do not match selected passes")
        primary_ids = {x["id"] for x in results["primary"]["findings"]}
        if {x["id"] for x in results["validation"]["outcomes"]} != primary_ids:
            raise ValueError("selected validation coverage")
        final_ids = primary_ids | {x["id"] for x in results["cold"]["findings"]} | {x["id"] for x in results["validation"]["new"]} | set(jobs["reconciliation"].get("previous_ids", []))
        if {x["id"] for x in results["reconciliation"]["outcomes"]} != final_ids:
            raise ValueError("selected reconciliation coverage")
        return results

    def accept(self, lock, pr, claim, intents):
        self.gate(lock, pr)
        active = self.claim(pr)
        if active != claim or claim["status"] != "active":
            raise ValueError("fenced acceptance")
        results = self.selected(pr, claim)
        # Persist the complete transaction recipe before any bundle writes.
        claim = dict(claim, prepared=intents)
        self.save_claim(lock, pr, claim)
        self.crash("prepared")
        return self._promote(lock, pr, claim, results)

    def _promote(self, lock, pr, claim, results):
        generation = claim["generation"]
        current = self.current(pr)
        if current and current["generation"] >= generation:
            return current
        intents = copy.deepcopy(claim["prepared"])
        # Retain mutable destinations even if an old fan-out never reached the
        # index, except a page an operator retired as unreachable.
        seen = {(x["kind"], x["target"]) for x in intents}
        unreachable = set()
        for old in self.chain(pr):
            for intent in old["intents"]:
                identity = intent["kind"], intent["target"]
                if identity in seen or intent.get("retained") or intent["kind"] not in ("publish", "projection", "annotation"):
                    continue
                if intent["target"] not in unreachable:
                    rows = read(self.root / "membership" / (digest(canonical(intent["target"])) + ".json"), {})
                    if rows.get(pr, {}).get("unreachable"):
                        unreachable.add(intent["target"])
                if intent["target"] not in unreachable:
                    inherited = copy.deepcopy({k: v for k, v in intent.items() if k != "id"})
                    if inherited["kind"] == "projection" and pr in inherited.get("patches", {}):
                        inherited["patches"][pr]["generation"] = generation
                    intents.append(inherited)
                    seen.add(identity)
        for intent in intents:
            intent["id"] = delivery_id(pr, generation, intent["kind"], intent["target"])
        publications = [intent["id"] for intent in intents
                        if intent["kind"] == "publish" and not intent.get("landing")]
        for intent in intents:
            if intent["kind"] in ("comment", "note") and "dependencies" in intent:
                intent["dependencies"] = list(dict.fromkeys(intent.get("dependencies", []) + publications))
        bundle = {"schema": 1, "pr": canonical(pr), "generation": generation,
                  "run": claim["run"], "request": claim["request"], "inputs": claim["inputs"],
                  "selected": claim["selected"],
                  "reconciliation_snapshot": read(Path(claim["selected"]["reconciliation"]) / "job.json").get("fresh_snapshot", {}),
                  "results": results, "previous": current, "intents": intents}
        parent = self.pr_dir(pr) / "generations"
        mkdir(parent)
        dest = parent / str(generation)
        if not dest.exists():
            temp = parent / (".bundle-" + uuid.uuid4().hex)
            mkdir(temp)
            atomic(temp / "bundle.json", bundle, self.crash, "bundle")
            # Copy evidence into the immutable bundle before promotion.
            for kind, result in results.items():
                source = Path(claim["selected"][kind]).resolve()
                for relative in set(evidence_paths(result)):
                    src = (source / relative).resolve()
                    if not src.is_relative_to(source) or not src.is_file():
                        raise ValueError("missing or external evidence")
                    target = temp / "evidence" / kind / relative
                    mkdir(target.parent)
                    with open(src, "rb") as inp, open(target, "xb") as out:
                        shutil.copyfileobj(inp, out)
                        out.flush()
                        os.fsync(out.fileno())
                    self.crash("evidence_file")
                    fsync_dir(target.parent)
                    self.crash("evidence_fsync")
            fsync_dir(temp)
            self.crash("bundle_complete")
            os.rename(temp, dest)
            self.crash("bundle_directory_rename")
            fsync_dir(parent)
            self.crash("bundle_directory_fsync")
        elif read(dest / "bundle.json") != bundle:
            raise ValueError("generation already contains different bundle")
        if self.claim(pr) != claim:
            raise ValueError("claim changed before promotion")
        self.crash("claim_checked")
        pointer = {"generation": generation, "digest": bundle_digest(dest)}
        atomic(self.pr_dir(pr) / "current", pointer, self.crash, "current")
        self.rebuild(lock, pr)
        return pointer

    # Receipts: one file each when written; old ones are moved into a SQLite
    # ledger by the collector. Every reader goes through these methods, which
    # look at the file first and then the ledger, so a receipt is always
    # found wherever it lives, and nothing infers absence from a missing file.

    def ledger(self):
        import sqlite3
        if getattr(self, "_ledger", None) is None:
            db = sqlite3.connect(str(self.root / "receipts.db"), timeout=60, check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS receipt (id TEXT PRIMARY KEY, body TEXT NOT NULL, "
                       "written REAL NOT NULL DEFAULT 0)")
            # a ledger from before write times were kept gains the column
            if "written" not in [row[1] for row in db.execute("PRAGMA table_info(receipt)")]:
                db.execute("ALTER TABLE receipt ADD COLUMN written REAL NOT NULL DEFAULT 0")
            self._ledger = db
        return self._ledger

    def receipt_of(self, ident, default=None):
        record = read(self.root / "receipts" / (ident + ".json"))
        if record is not None:
            return record
        if not (self.root / "receipts.db").exists():
            return default
        row = self.query("SELECT body FROM receipt WHERE id = ?", (ident,))
        return json.loads(row[0]) if row else default

    def receipt_time(self, ident):
        """When a receipt was written, from its file or the ledger, or None."""
        try:
            return (self.root / "receipts" / (ident + ".json")).stat().st_mtime
        except OSError:
            pass
        if not (self.root / "receipts.db").exists():
            return None
        row = self.query("SELECT written FROM receipt WHERE id = ?", (ident,))
        return row[0] if row and row[0] else None

    def query(self, sql, args):
        import sqlite3
        try:
            return self.ledger().execute(sql, args).fetchone()
        except sqlite3.Error as error:
            raise OSError("receipt ledger: %s" % error) from error

    def has_receipt(self, ident):
        return (self.root / "receipts" / (ident + ".json")).exists() or self.receipt_of(ident) is not None

    def receipts(self):
        """Every receipt, files and ledger; for the rare full scan. An
        unreadable receipt raises: no scan may take it as absent."""
        seen = set()
        for path in (self.root / "receipts").glob("*.json"):
            record = read(path)
            if record:
                seen.add(path.stem)
                yield record
        if (self.root / "receipts.db").exists():
            import sqlite3
            try:
                rows = self.ledger().execute("SELECT id, body FROM receipt").fetchall()
            except sqlite3.Error as error:
                raise OSError("receipt ledger: %s" % error) from error
            for ident, body in rows:
                if ident not in seen:
                    yield json.loads(body)

    def compact_receipts(self, older_than, limit=10000, deadline=None):
        """Move receipt files last changed before older_than into the ledger:
        each batch is committed, durably, before its files are removed, so
        a receipt is in at least one place at every moment. Returns how many
        moved."""
        import sqlite3
        moved, batch = 0, []
        try:
            db = self.ledger()
        except sqlite3.Error as error:
            raise OSError("receipt ledger: %s" % error) from error
        for entry in os.scandir(self.root / "receipts"):
            if moved + len(batch) >= limit or (deadline and time.monotonic() >= deadline):
                break
            try:
                if not entry.name.endswith(".json") or entry.stat().st_mtime >= older_than:
                    continue
                written = entry.stat().st_mtime
                with open(entry.path) as f:
                    body = f.read()
                json.loads(body)
            except (OSError, ValueError):
                continue
            batch.append((entry.name[:-5], body, entry.path, written))
            if len(batch) >= 500:
                moved += self._commit_receipts(db, batch)
                batch = []
        if batch:
            moved += self._commit_receipts(db, batch)
        return moved

    @staticmethod
    def _commit_receipts(db, batch):
        import sqlite3
        try:
            with db:
                db.executemany("INSERT OR IGNORE INTO receipt (id, body, written) VALUES (?, ?, ?)",
                               [(ident, body, written) for ident, body, _, written in batch])
        except sqlite3.Error as error:
            raise OSError("receipt ledger: %s" % error) from error
        for _, _, path, _ in batch:
            try:
                os.unlink(path)
            except OSError:
                pass
        return len(batch)

    def receipt(self, entry, state, **details):
        record = {"id": entry["id"], "pr": entry["pr"], "generation": entry["generation"],
                  "kind": entry["kind"], "target": entry["target"], "state": state, **details}
        atomic(self.root / "receipts" / (entry["id"] + ".json"), record, self.crash, "receipt")
        unlink(self.root / "outbox" / (entry["id"] + ".json"))
        self.crash("outbox_removed")

    def rebuild(self, lock, pr):
        self.gate(lock, pr)
        current = self.current(pr)
        for bundle in self.chain(pr):
            for intent in bundle["intents"]:
                self.materialize(pr, bundle["generation"], intent, current["generation"])

    def materialize(self, pr, generation, intent, current):
        ident = intent["id"]
        path = self.root / "outbox" / (ident + ".json")
        if self.has_receipt(ident):
            unlink(path)
            return
        # created never changes: the age of an obligation, unlike the file's
        # mtime, which every retry rewrites
        entry = read(path) or dict(intent, pr=pr, generation=generation, state="owed",
                                   failures=0, next_attempt=0, created=time.time())
        if generation != current and intent["kind"] in ("comment", "note") and entry["state"] not in ("sending", "uncertain"):
            self.receipt(entry, "superseded")
        elif not path.exists():
            atomic(path, entry, self.crash, "intent")

    def recover_pr(self, lock, pr):
        self.gate(lock, pr)
        parent = self.pr_dir(pr) / "generations"
        for temp in self.pr_dir(pr).glob(".*"):
            if temp.is_file():
                unlink(temp)
        for temp in parent.glob(".bundle-*"):
            shutil.rmtree(temp)
            fsync_dir(parent)
        claim = self.claim(pr)
        if claim and claim.get("status") == "active" and "prepared" in claim:
            try:
                results = self.selected(pr, claim)
            except (ValueError, OSError, KeyError):
                pass
            else:
                self._promote(lock, pr, claim, results)
        self.rebuild(lock, pr)

    def ticket(self):
        lock = acquire(self.locks, "observation", time.monotonic() + 5)
        if lock is None:
            raise TimeoutError("observation counter busy")
        with lock:
            path = self.root / "observation.json"
            value = read(path, 0) + 1
            atomic(path, value)
            return value

    def merge_membership(self, page, patches, deadline=None):
        lock = acquire(self.locks, page, deadline or time.monotonic() + 5)
        if lock is None:
            raise TimeoutError("membership busy")
        with lock:
            return self._merge_membership(lock, page, patches)

    def _merge_membership(self, lock, page, patches, write=True):
        """Merge patches into a page's membership. With write false, return
        what the rows would become and change nothing."""
        if lock.closed or lock.region != region(page):
            raise RuntimeError("membership page ownership required")
        path = self.root / "membership" / (digest(canonical(page)) + ".json")
        rows = read(path, {})
        for pr, patch in patches.items():
            old = rows.get(pr, {"ticket": -1, "removed": True})
            row = dict(old)
            if "ticket" in patch and patch["ticket"] > old["ticket"]:
                row.update(ticket=patch["ticket"], removed=patch["removed"])
                row.pop("candidate", None)
                for field in ("ci", "progress", "unreachable"):
                    if field in patch:
                        row[field] = patch[field]
                current = self.current(pr)
                if not row["removed"] and current:
                    row["generation"] = max(row.get("generation", 0), current["generation"])
            if "generation" in patch and not row["removed"]:
                generation = patch["generation"]
                accepted = {b["generation"] for b in self.chain(pr)}
                if generation not in accepted:
                    raise ValueError("membership before promotion")
                if generation >= row.get("generation", 0):
                    row["generation"] = generation
            rows[pr] = row
        if write:
            atomic(path, rows, self.crash, "membership")
        return rows

    def record_published(self, lock, page, result, destination):
        """Store, on each row, what the page served by this verified upload
        shows for it and where: a controller suppresses an observation only
        when the page would show exactly that. Caller holds the page."""
        views = result.get("views")
        if views is None or lock is None or lock.closed or lock.region != region(page):
            return
        path = self.root / "membership" / (digest(canonical(page)) + ".json")
        rows = read(path)
        if not rows:
            return
        for pr, row in rows.items():
            if not isinstance(row, dict):
                continue
            # the epoch names the upload; any later one, of any kind and even
            # one that died, moves it and makes this record stale
            row["published"] = {"view": views.get(pr), "destination": destination,
                                "epoch": result.get("epoch")}
        atomic(path, rows, self.crash, "membership")

    def write_membership(self, lock, page, rows):
        """Write rows worked out under this same page lock (a dry merge)."""
        if lock.closed or lock.region != region(page):
            raise RuntimeError("membership page ownership required")
        atomic(self.root / "membership" / (digest(canonical(page)) + ".json"), rows, self.crash, "membership")

    def journal(self, run, phase, pr, intents):
        if any(x["kind"] not in ("publish", "projection") or x.get("gate") != "page" or
               not x["target"].startswith("page:") for x in intents):
            raise ValueError("operation journals contain only standalone page intents")
        ident = digest([run, phase, canonical(pr)])
        path = self.root / "operations" / (ident + ".json")
        operation = {"id": ident, "pr": canonical(pr), "intents": [dict(x, id=delivery_id(pr, ident, x["kind"], x["target"])) for x in intents]}
        lock = acquire(self.locks, "page:operations/" + ident, time.monotonic() + 5)
        if lock is None:
            raise TimeoutError("operation busy")
        with lock:
            old = read(path)
            if old and old != operation:
                raise ValueError("operation identity reused")
            if not old:
                atomic(path, operation, self.crash, "operation")
        # Fan out now where the page is free. Recovery is only the fallback
        # for a controller that dies here: left to it alone, a new journal
        # waited behind thousands of old ones for its outbox entries.
        for intent in operation["intents"]:
            try:
                page = try_lock(self.locks, intent["target"])
            except RuntimeError:
                page = None
            if page:
                with page:
                    self.materialize(operation["pr"], ident, intent, ident)
        return ident

    def snapshot(self):
        return ([str(p) for p in sorted((self.root / "results").glob("*/*/*/claim.json"))] +
                [str(p) for p in sorted((self.root / "operations").glob("*.json"))])

    def recover(self, snapshot=None, limit=100, seconds=60):
        deadline = time.monotonic() + seconds
        pending = list(self.snapshot() if snapshot is None else snapshot)
        done = 0
        try:
            recovered = read(self.root / "recovered.json", {})
        except (OSError, ValueError):
            recovered = {}              # an optimisation only: lose it, recover everything
        if not isinstance(recovered, dict):
            recovered = {}
        while pending and done < limit and time.monotonic() < deadline:
            item = pending.pop(0)
            done += 1
            try:
                path = Path(item)
                self._recover_one(path, deadline, pending, recovered)
            except Exception as error:      # one record's trouble is its own
                print("recovery: %s failed: %s" % (str(item)[:120], str(error)[:200]), file=sys.stderr)
                review_metrics.count("local", "recovery record failed")
        self.last_recovered = done
        atomic(self.root / "recovered.json", recovered)
        return pending

    def _recover_one(self, path, deadline, pending, recovered):
        if path.parent == self.root / "operations":
            operation = read(path)
            if not operation:
                return
            if all(self.has_receipt(i["id"]) for i in operation["intents"]):
                # finished: its receipts are the record; keep the walk short
                unlink(path)
                return
            for intent in operation["intents"]:
                if time.monotonic() >= deadline:
                    pending.insert(0, str(path))
                    break
                lock = try_lock(self.locks, intent["target"])
                if lock:
                    with lock:
                        self.materialize(operation["pr"], operation["id"], intent, operation["id"])
            return
        repo = path.parent.parent.parent.name + "/" + path.parent.parent.name
        pr = "pr:%s#%s" % (repo, path.parent.name)
        # A PR whose claim and current pointer have not changed since it
        # was last recovered has nothing new to recover.
        # A checkpoint lapses after RECOVERY_REVISIT, so every PR is rebuilt
        # from its generation chain that often whatever else happened
        # (an outbox entry lost, a timestamp that did not move).
        key = self.recovery_key(path, pr)
        mark = recovered.get(str(path))
        if (key is not None and isinstance(mark, list) and len(mark) == 2 and mark[0] == key
                and isinstance(mark[1], (int, float)) and time.time() - mark[1] < self.RECOVERY_REVISIT):
            return
        lock = try_lock(self.locks, pr)
        if lock is None:
            return
        with lock:
            if self.clean_owner(lock, deadline):
                self.recover_pr(lock, pr)
                recovered[str(path)] = [self.recovery_key(path, pr), time.time()]


    RECOVERY_REVISIT = 6 * 3600

    def recovery_key(self, claim_path, pr):
        """What would make a PR need recovery again: its claim, its current
        pointer, or its owner record (payloads to clean) changing."""
        parts = []
        for path in (claim_path, claim_path.parent / "current", self.root / "owners" / (str(region(pr)) + ".json")):
            try:
                parts.append(path.stat().st_mtime_ns)
            except FileNotFoundError:
                parts.append(0)
            except OSError:
                return None
        return parts

    def recover_slice(self, limit=100, seconds=30):
        """One bounded slice of recovery for a caller with no controller of
        its own (the cron drain): journalled operations a dead controller
        never fanned out, and PRs left mid-promotion. The cursor persists so
        successive calls walk the whole store."""
        path = self.root / "drain-recovery.json"
        try:
            cursor = read(path)
        except (OSError, ValueError):
            cursor = None
        pending = self.recover(cursor if isinstance(cursor, list) else None, limit=limit, seconds=seconds)
        atomic(path, pending or None)
        return pending

    # Dependencies first, so a bounded pass over a large outbox is not a
    # window of publishes all waiting on projections outside it.
    KIND_ORDER = {"projection": 0, "publish": 1, "comment": 2, "note": 2, "annotation": 3, "deprecate": 3, "board": 4}

    def delivery_snapshot(self, limit=100):
        # another drain may receipt and remove an entry between glob and read;
        # an entry that is unreadable or malformed is reported and left as it
        # is, never allowed to stop the selection of every other entry
        entries = []
        for p in (self.root / "outbox").glob("*.json"):
            try:
                x = read(p)
            except (OSError, ValueError) as error:
                print("drain: unreadable outbox entry %s: %s" % (p.name, error), file=sys.stderr)
                continue
            if x is None:
                continue
            gate = x.get("gate", "pr") if isinstance(x, dict) else None
            if not (isinstance(x, dict) and x.get("id") == p.stem and isinstance(x.get("kind"), str)
                    and isinstance(x.get("next_attempt", 0), (int, float))
                    and isinstance(x.get("failures", 0), int)
                    and isinstance(x.get("dependencies", []), list)
                    and all(isinstance(d, str) for d in x.get("dependencies", []))
                    # the key its lock is taken on must exist, or it could
                    # never even be quarantined and would block every pass
                    and self.valid_key(x.get("target" if gate == "page" else "pr"))
                    and x.get("target" if gate == "page" else "pr", "").startswith("page:" if gate == "page" else "pr:")):
                print("drain: malformed outbox entry %s" % p.name, file=sys.stderr)
                review_metrics.count("local", "malformed outbox entry")
                continue
            entries.append(x)
        snapshot = sorted(entries, key=lambda x: (x.get("next_attempt", 0), self.KIND_ORDER.get(x["kind"], 5), x["id"]))
        # Only entries whose dependencies have settled: a blocked entry keeps
        # its place at the head, and a hundred of them filled every pass while
        # ready entries behind them waited.
        # A dependency queued and itself ready counts: it settles earlier in
        # the same pass (projections sort before the publishes they feed).
        now = time.time()
        queued = {x["id"]: x for x in snapshot}
        settled = {}
        receipted = set()

        def ok(dep):
            if dep not in settled:
                settled[dep] = False            # a cycle, or a receipt it cannot read, is not ready
                try:
                    receipt = self.receipt_of(dep)
                except (OSError, ValueError):
                    return False
                if isinstance(receipt, dict):
                    settled[dep] = receipt.get("state") in self.SETTLED
                    if settled[dep]:
                        receipted.add(dep)
                elif dep in queued:
                    settled[dep] = ready(queued[dep])
            return settled[dep]

        def ready(x):
            try:
                return (x.get("next_attempt", 0) <= now and x.get("failures", 0) < 5
                        and all(ok(d) or self.unsatisfiable(x, d) for d in x.get("dependencies", [])))
            except Exception:
                return False

        # Entries whose dependencies are already settled come first, then those
        # ready only through a queued dependency: a batch cut at the limit
        # never holds dependents while what they wait on is left outside.
        chosen = [x for x in snapshot if ready(x)]
        def waits(x):
            try:
                return any(d not in receipted and not self.unsatisfiable(x, d)
                           for d in x.get("dependencies", []))
            except Exception:
                return True
        chosen.sort(key=waits)
        return chosen[:limit]

    SETTLED = ("published", "posted", "not_applicable", "synced", "held", "superseded")

    @staticmethod
    def valid_key(key):
        try:
            return isinstance(key, str) and bool(canonical(key))
        except ValueError:
            return False

    def unsatisfiable(self, entry, dep):
        """A projection's dependency that can never settle: no receipt, no
        outbox entry and no journalled operation that could produce one.
        Generations accepted before 860dc32 wait on discovery projections
        that were never journalled. A projection is a ticket-ordered
        membership merge, so it may go ahead, but only when every PR it
        patches already has a live row on the page or the patch carries a
        whole row: otherwise the missing projection is what would have added
        the PR, and the page would go out without it. Nothing else ever
        treats a missing dependency as met."""
        if entry.get("kind") != "projection":
            return False
        patches = entry.get("patches")
        if not isinstance(patches, dict) or not isinstance(entry.get("target"), str):
            return False
        try:
            rows = read(self.root / "membership" / (digest(canonical(entry["target"])) + ".json"), {})
        except (OSError, ValueError):
            return False
        if not isinstance(rows, dict):
            return False
        for pr, patch in patches.items():
            row = rows.get(pr)
            whole = isinstance(patch, dict) and "ticket" in patch and "removed" in patch
            if not whole and not (isinstance(row, dict) and row.get("removed") is False):
                return False
        if self.has_receipt(dep) or (self.root / "outbox" / (dep + ".json")).exists():
            return False
        for path in (self.root / "operations").glob("*.json"):
            try:
                if any(i.get("id") == dep for i in (read(path) or {}).get("intents", [])):
                    return False
            except (OSError, ValueError):
                return False            # cannot tell: keep waiting
        return True

    def _coalesce(self, entry, result, adapter, deadline=None, lock=None):
        """One publish of a page satisfies every other owed publish of it that
        was ready before the page was rendered again here, when that render
        is byte for byte what was uploaded: the upload then shows everything
        those entries wait on. Caller holds the page region."""
        if deadline and time.monotonic() >= deadline:
            return
        target = canonical(entry["target"])
        batch = []
        for path in (self.root / "outbox").glob("*.json"):
            try:
                other = read(path)
                if (not isinstance(other, dict) or other.get("id") == entry["id"] or other.get("kind") != "publish"
                        or other.get("gate") != "page" or canonical(other["target"]) != target
                        or other.get("retained") or not self.same_destination(adapter, entry, other)):
                    continue
                if self.has_receipt(other["id"]):
                    unlink(path)
                    continue
                if self.settled(other):
                    batch.append(other)
            except Exception:
                continue                # another entry's trouble is not this one's
        # the batch is frozen before the render, so it can only have seen
        # their dependencies; anything settling later waits its own turn
        if deadline and time.monotonic() >= deadline:
            return
        if not batch or not self.current_matches(adapter, entry, result):
            return
        for other in batch:
            if deadline and time.monotonic() >= deadline:
                return
            self.settle_from(other, result, lock, self.destination(adapter, other))

    @staticmethod
    def destination(adapter, entry):
        where = getattr(adapter, "destination_of", None)
        return where(entry) if where else None

    @staticmethod
    def same_destination(adapter, entry, other):
        """Entries carry their own frozen endpoint configuration; an upload
        settles another entry only if both go to the same place."""
        where = getattr(adapter, "destination_of", None)
        return where is None or where(entry) == where(other)

    def settle_from(self, entry, result, lock=None, destination=None):
        """Receipt entry from another upload of its page, with its own state:
        superseded when its PR has left the page, as delivery decides it."""
        rows = read(self.root / "membership" / (digest(canonical(entry["target"])) + ".json"), {})
        removed = (isinstance(entry["generation"], int) and not entry.get("retained")
                   and rows.get(entry["pr"], {}).get("removed"))
        self.record_published(lock, entry["target"], result, destination)
        self.receipt(entry, **dict(result, state="superseded" if removed else "published"))

    def settled(self, entry):
        for dep in entry.get("dependencies", []):
            receipt = self.receipt_of(dep)
            if not receipt or receipt["state"] not in self.SETTLED:
                return False
        return True

    def contained(self, entry, result, adapter):
        """A ready publish is satisfied by an earlier upload of its page when
        that upload is still the page's latest publication and the page
        rendered now, after the entry's dependencies settled, has the same
        bytes. Membership alone is not proof, since a page also shows claims
        and receipts; nor are receipt timestamps, since a receipt can be
        renamed into place after a render read the directory."""
        return self.settled(entry) and self.current_matches(adapter, entry, result)

    @staticmethod
    def current_matches(adapter, entry, result):
        try:
            now = getattr(adapter, "current", lambda e: None)(entry)
        except (OSError, KeyError, ValueError):
            return False
        if not now or not result.get("page_digest") or now.get("page_digest") != result["page_digest"]:
            return False
        # no upload of the page, successful or not, since this one
        return all(now.get(k) == result[k] for k in ("revision", "epoch") if k in result)

    def side_locks(self, entry, dependencies, gate):
        """Keep destination ownership through both the adapter call and receipt."""
        stack = ExitStack()
        if entry.get("gate") == "page":
            return stack, {gate.region: gate}
        keys = []
        if entry["kind"] in ("publish", "projection", "annotation"):
            keys.append(entry["target"])
        elif entry["kind"] in ("comment", "note"):
            keys += [dep["target"] for dep in dependencies if dep["kind"] == "publish"]
        elif entry["kind"] == "board":
            keys.append("board")
        locks = {}
        try:
            for offset, key in sorted({region(key): key for key in keys}.items()):
                lock = try_lock(self.locks, key)
                if lock is None:
                    raise TimeoutError("delivery region busy")
                locks[offset] = stack.enter_context(lock)
            return stack, locks
        except BaseException:
            stack.close()
            raise

    ENTRY_SECONDS = 60

    def drain(self, adapter, limit=100, seconds=60, snapshot=None):
        deadline = time.monotonic() + seconds
        snapshot = self.delivery_snapshot(limit) if snapshot is None else snapshot[:limit]
        budget = deadline
        for selected in snapshot:
            if time.monotonic() >= budget:
                break
            # One entry's trouble is that entry's: a malformed record or an
            # unexpected error is recorded on it, and the drain carries on.
            try:
                self._drain_one(adapter, selected, budget)
            except Exception as error:
                self.quarantine(selected, error)
        owed = []
        for p in sorted((self.root / "outbox").glob("*.json")):
            try:
                x = read(p)
            except (OSError, ValueError):
                continue
            if isinstance(x, dict):
                owed.append(x)
        return owed

    def quarantine(self, selected, error):
        """Record an unexpected failure on the one entry it struck, keeping
        its state (an uncertain comment stays uncertain), and back it off; an
        entry that cannot even be read is reported and left as it is."""
        import traceback
        ident = selected.get("id") if isinstance(selected, dict) else None
        print("drain: entry %s failed: %s" % (ident, "".join(
            traceback.format_exception_only(type(error), error)).strip()[:300]), file=sys.stderr)
        review_metrics.count("local", "drain entry failed")
        if not ident:
            return
        path = self.root / "outbox" / (str(ident) + ".json")
        try:
            gate = selected.get("gate", "pr")
            lock = try_lock(self.locks, selected["target"] if gate == "page" else selected["pr"])
        except Exception:
            lock = None
        if lock is None:
            return
        with lock:
            try:
                entry = read(path)
                if not isinstance(entry, dict):
                    return
                # capped below the give-up count: an unexpected failure keeps
                # being retried, and works again once the cause is fixed
                entry["failures"] = min(4, int(entry.get("failures", 0)) + 1)
                entry["next_attempt"] = time.time() + min(3600, 60 * 2 ** (entry["failures"] - 1))
                entry["error"] = "unexpected: %s" % str(error)[:200]
                atomic(path, entry)
            except Exception:
                pass

    def _drain_one(self, adapter, selected, budget):
        # The budget decides only whether to start an entry; one started
        # gets a full minute. Handing it what was left of a short budget
        # failed publishes and comments on a deadline until they gave up.
        deadline = max(budget, time.monotonic() + self.ENTRY_SECONDS)
        delivered = False
        gate = selected.get("gate", "pr")
        key = selected["target"] if gate == "page" else selected["pr"]
        lock = try_lock(self.locks, key)
        if lock is None:
            return
        with lock:
            if gate == "pr":
                if not self.clean_owner(lock, deadline):
                    return
                self.rebuild(lock, selected["pr"])
            path = self.root / "outbox" / (selected["id"] + ".json")
            entry = read(path)
            if not entry or self.has_receipt(entry["id"]):
                unlink(path)
                return
            if entry["failures"] >= 5 or entry["next_attempt"] > time.time():
                return
            dependencies = [self.receipt_of(dep) for dep in entry.get("dependencies", [])
                            if not self.unsatisfiable(entry, dep)]
            if any(not dep or dep["state"] not in self.SETTLED for dep in dependencies):
                return
            if entry["kind"] in ("comment", "note"):
                # an earlier comment still in flight goes first; an entry that
                # might be one but cannot be read or ordered holds this comment
                # (only this one: the drain carries on)
                for p in (self.root / "outbox").glob("*.json"):
                    try:
                        x = read(p)
                    except (OSError, ValueError):
                        return
                    if not (isinstance(x, dict) and isinstance(x.get("pr"), str)
                            and isinstance(x.get("kind"), str) and isinstance(x.get("state"), str)):
                        return
                    if x.get("pr") == entry["pr"] and x.get("kind") in ("comment", "note"):
                        if not (isinstance(x.get("generation"), int) and isinstance(entry["generation"], int)):
                            return
                        if x["generation"] < entry["generation"] and x["state"] in ("sending", "uncertain"):
                            return
            credentials = ExitStack()
            try:
                if hasattr(adapter, "credentials"):
                    credentials = adapter.credentials(entry, deadline)
                side_stack, side_locks = self.side_locks(entry, dependencies, lock)
            except TimeoutError:
                credentials.close()
                return
            except Exception:
                credentials.close()     # a lease must never outlive this entry
                raise
            try:
                if entry["kind"] == "projection":
                    self._merge_membership(side_locks[region(entry["target"])], entry["target"], entry["patches"])
                    result = {"state": "published"}
                else:
                    if entry["state"] in ("sending", "uncertain"):
                        current = self.current(entry["pr"])
                        entry["superseded"] = bool(current and isinstance(entry["generation"], int) and current["generation"] > entry["generation"])
                        result = adapter.reconcile(entry, deadline)
                        if result is None:
                            return
                    else:
                        current = self.current(entry["pr"])
                        if current and entry["kind"] in ("publish", "board", "annotation"):
                            entry["effective_generation"] = entry["generation"] if entry.get("retained") else current["generation"]
                        prepared = adapter.prepare(entry, deadline) if hasattr(adapter, "prepare") else None
                        if prepared is not None:
                            self.receipt(entry, **prepared)
                            return
                        # A page renders its whole membership, so a render
                        # begun after this entry's projection settled already
                        # carries it; each re-render took seconds and an all
                        # run owes dozens of the same page.
                        whole = entry["kind"] == "publish" and not entry.get("retained")
                        target = canonical(entry["target"]) if whole else None
                        # the page's last verified upload to this entry's
                        # destination, proven by rendering now
                        done = getattr(adapter, "confirmed", lambda e: None)(entry) if whole else None
                        if done and self.contained(entry, done, adapter):
                            self.settle_from(entry, done,
                                             lock if gate == "page" else side_locks.get(region(entry["target"])),
                                             self.destination(adapter, entry))
                            review_metrics.count("local", "publish settled by a render")
                            return
                        delivered = True
                        entry["state"] = "sending"
                        entry["payload_digest"] = digest(entry.get("payload", {}))
                        atomic(path, entry)
                        result = adapter.deliver(entry, deadline)
                        self.crash("remote_effect")
                if entry["kind"] == "publish" and not entry.get("retained"):
                    # before the receipt: a crash between leaves the entry
                    # owed, never a receipted upload with an older record
                    page_lock = lock if gate == "page" else side_locks.get(region(entry["target"]))
                    self.record_published(page_lock, entry["target"], result,
                                          self.destination(adapter, entry))
                self.receipt(entry, **result)
                if entry["kind"] == "publish" and entry.get("gate") == "page" and delivered:
                    self._coalesce(entry, result, adapter, deadline, lock)
            except (OSError, TimeoutError) as error:
                entry["failures"] += 1
                entry["next_attempt"] = time.time() + min(3600, 60 * 2 ** (entry["failures"] - 1))
                entry["error"] = str(error)
                # A lost response must go through reconciliation.
                if entry["state"] == "sending":
                    entry["state"] = "uncertain"
                atomic(path, entry)
            finally:
                side_stack.close()
                credentials.close()



class StubAdapter:
    """A durable fake remote makes the lost-response boundary testable."""
    def __init__(self, root):
        self.root = Path(root) / "stub-deliveries"
        mkdir(self.root)

    def deliver(self, entry, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError("delivery deadline")
        states = {"publish": "published", "comment": "posted", "board": "synced",
                  "deprecate": "deprecated", "note": "posted"}
        result = {"state": entry.get("outcome", states.get(entry["kind"], "published")),
                  "payload_digest": entry["payload_digest"], "target_id": entry["id"]}
        if entry["kind"] == "publish":
            result["page_digest"] = self.current(entry)["page_digest"]
            if not entry.get("retained") and not entry.get("landing"):
                from review_render import Renderer
                renderer = Renderer(Store(self.root.parent))
                try:
                    renderer.body(canonical(entry["target"]))
                    result["views"] = renderer.views
                except (OSError, KeyError, ValueError, IndexError):
                    pass
            if not entry.get("retained"):
                atomic(self.root / "confirmed" / (digest(canonical(entry["target"])) + ".json"), result)
        atomic(self.root / (entry["id"] + ".json"), {"entry": entry, "result": result})
        return result

    def confirmed(self, entry):
        if entry.get("retained"):
            return None
        return read(self.root / "confirmed" / (digest(canonical(entry["target"])) + ".json"))

    def current(self, entry):
        """The stub's page is its membership, and for a landing page the label
        publishes it can see, as the real renderer reads them."""
        path = self.root.parent / "membership" / (digest(canonical(entry["target"])) + ".json")
        rows = read(path)
        # a page renders its rows, not the views delivery records on them
        page = json.dumps({pr: {k: v for k, v in row.items() if k != "published"}
                           for pr, row in rows.items()}, sort_keys=True) if rows is not None else "empty"
        if entry.get("landing"):
            page += " ".join(sorted(p.name for p in (self.root.parent / "receipts").glob("*.json")))
        return dict(page_digest=digest(page))

    def reconcile(self, entry, deadline):
        old = read(self.root / (entry["id"] + ".json"))
        if old:
            if old["entry"]["payload_digest"] != entry["payload_digest"]:
                return None
            return old["result"]
        if entry.get("superseded") and entry["kind"] in ("comment", "note"):
            return {"state": "superseded"}
        # This adapter is local and synchronous: absence is authoritative.
        return self.deliver(entry, deadline)

"""Promotion is the commit point; indices can always be rebuilt from its chain."""
import hashlib
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import time
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
        if bundle_digest(path) != pointer["digest"]:
            raise ValueError("bundle digest mismatch")
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
        atomic(path, claim)
        atomic(self.owner_path(lock), {"registry": str(path), "pr": canonical(pr)})
        self.crash("allocated")
        return claim

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
            if (status.get("state") != "terminal" or status.get("exit") != 0 or
                    status.get("timed_out") or status.get("aborted") or
                    status.get("result_status") != "complete" or not status.get("empty") or
                    any(status.get(k) != job[k] for k in IDENTITY) or
                    job["generation"] != claim["generation"] or job["run"] != claim["run"] or
                    job["kind"] != kind or job["input_digest"] != digest(claim["inputs"])):
                raise ValueError("unsuccessful or fenced attempt")
            result = read_result(Path(path) / FILES[kind], job)
            if result["status"] != "complete":
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

    def receipt_index(self):
        """Receipts are immutable; parse only newly observed files."""
        if not hasattr(self, "_receipts"):
            self._receipts = {}
        for path in (self.root / "receipts").glob("*.json"):
            if path.stem not in self._receipts:
                self._receipts[path.stem] = read(path)
        return self._receipts

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
        if (self.root / "receipts" / (ident + ".json")).exists():
            unlink(path)
            return
        entry = read(path) or dict(intent, pr=pr, generation=generation, state="owed",
                                   failures=0, next_attempt=0)
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

    def _merge_membership(self, lock, page, patches):
        if lock.closed or lock.region != region(page):
            raise RuntimeError("membership page ownership required")
        path = self.root / "membership" / (digest(canonical(page)) + ".json")
        rows = read(path, {})
        for pr, patch in patches.items():
            old = rows.get(pr, {"ticket": -1, "removed": True})
            row = dict(old)
            if "ticket" in patch and patch["ticket"] > old["ticket"]:
                row.update(ticket=patch["ticket"], removed=patch["removed"])
                for field in ("ci", "progress", "candidate", "unreachable"):
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
        atomic(path, rows, self.crash, "membership")
        return rows

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
        return ident

    def snapshot(self):
        return ([str(p) for p in sorted((self.root / "results").glob("*/*/*/claim.json"))] +
                [str(p) for p in sorted((self.root / "operations").glob("*.json"))])

    def recover(self, snapshot=None, limit=100, seconds=60):
        deadline = time.monotonic() + seconds
        pending = list(self.snapshot() if snapshot is None else snapshot)
        done = 0
        while pending and done < limit and time.monotonic() < deadline:
            path = Path(pending.pop(0))
            done += 1
            if path.parent == self.root / "operations":
                operation = read(path)
                for intent in operation["intents"]:
                    if time.monotonic() >= deadline:
                        pending.insert(0, str(path))
                        break
                    lock = try_lock(self.locks, intent["target"])
                    if lock:
                        with lock:
                            self.materialize(operation["pr"], operation["id"], intent, operation["id"])
                continue
            repo = path.parent.parent.parent.name + "/" + path.parent.parent.name
            pr = "pr:%s#%s" % (repo, path.parent.name)
            lock = try_lock(self.locks, pr)
            if lock is None:
                continue
            with lock:
                if self.clean_owner(lock, deadline):
                    self.recover_pr(lock, pr)
        self.last_recovered = done
        return pending

    # Dependencies first, so a bounded pass over a large outbox is not a
    # window of publishes all waiting on projections outside it.
    KIND_ORDER = {"projection": 0, "publish": 1, "comment": 2, "note": 2, "annotation": 3, "deprecate": 3, "board": 4}

    def delivery_snapshot(self, limit=100):
        snapshot = sorted((read(p) for p in (self.root / "outbox").glob("*.json")),
                          key=lambda x: (x["next_attempt"], self.KIND_ORDER.get(x["kind"], 5), x["id"]))
        return [x for x in snapshot if x["next_attempt"] <= time.time() and x["failures"] < 5][:limit]

    SETTLED = ("published", "posted", "not_applicable", "synced", "held", "superseded")

    def _coalesce(self, entry, result):
        """One publish of a page satisfies every other owed publish of it whose
        projection is already merged: the page renders the whole membership,
        and every progress step of every PR on it asked for the same thing.
        Caller holds the page region."""
        target = canonical(entry["target"])
        for path in (self.root / "outbox").glob("*.json"):
            other = read(path)
            if (not other or other["id"] == entry["id"] or other["kind"] != "publish"
                    or other.get("gate") != "page" or canonical(other["target"]) != target):
                continue
            if (self.root / "receipts" / (other["id"] + ".json")).exists():
                unlink(path)
                continue
            dependencies = [read(self.root / "receipts" / (d + ".json")) for d in other.get("dependencies", [])]
            if any(not d or d["state"] not in self.SETTLED for d in dependencies):
                continue
            self.receipt(other, **result)

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

    def drain(self, adapter, limit=100, seconds=60, snapshot=None):
        deadline = time.monotonic() + seconds
        snapshot = self.delivery_snapshot(limit) if snapshot is None else snapshot[:limit]
        for selected in snapshot:
            if time.monotonic() >= deadline:
                break
            gate = selected.get("gate", "pr")
            key = selected["target"] if gate == "page" else selected["pr"]
            lock = try_lock(self.locks, key)
            if lock is None:
                continue
            with lock:
                if gate == "pr":
                    if not self.clean_owner(lock, deadline):
                        continue
                    self.rebuild(lock, selected["pr"])
                path = self.root / "outbox" / (selected["id"] + ".json")
                entry = read(path)
                if not entry or (self.root / "receipts" / (entry["id"] + ".json")).exists():
                    unlink(path)
                    continue
                if entry["failures"] >= 5 or entry["next_attempt"] > time.time():
                    continue
                dependencies = [read(self.root / "receipts" / (dep + ".json")) for dep in entry.get("dependencies", [])]
                if any(not dep or dep["state"] not in self.SETTLED for dep in dependencies):
                    continue
                if entry["kind"] in ("comment", "note"):
                    older = [read(p) for p in (self.root / "outbox").glob("*.json")]
                    if any(x["pr"] == entry["pr"] and x["kind"] in ("comment", "note") and x["generation"] < entry["generation"] and x["state"] in ("sending", "uncertain") for x in older):
                        continue
                credentials = ExitStack()
                try:
                    if hasattr(adapter, "credentials"):
                        credentials = adapter.credentials(entry, deadline)
                    side_stack, side_locks = self.side_locks(entry, dependencies, lock)
                except TimeoutError:
                    credentials.close()
                    continue
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
                                continue
                        else:
                            current = self.current(entry["pr"])
                            if current and entry["kind"] in ("publish", "board", "annotation"):
                                entry["effective_generation"] = entry["generation"] if entry.get("retained") else current["generation"]
                            prepared = adapter.prepare(entry, deadline) if hasattr(adapter, "prepare") else None
                            if prepared is not None:
                                self.receipt(entry, **prepared)
                                continue
                            entry["state"] = "sending"
                            entry["payload_digest"] = digest(entry.get("payload", {}))
                            atomic(path, entry)
                            result = adapter.deliver(entry, deadline)
                            self.crash("remote_effect")
                    self.receipt(entry, **result)
                    if entry["kind"] == "publish" and entry.get("gate") == "page":
                        self._coalesce(entry, result)
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
        return [read(p) for p in sorted((self.root / "outbox").glob("*.json"))]


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
        atomic(self.root / (entry["id"] + ".json"), {"entry": entry, "result": result})
        return result

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

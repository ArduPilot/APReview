"""Drained, restartable repository transfer using a complete publication mirror.

The mirror is retained as the rollback/export artefact. Remote replacement is
done under page regions before routing commits. No comment/board writes here.
"""
from contextlib import ExitStack
import fcntl
import json
import sys
from html import escape, unescape
import os
from pathlib import Path
import re
import subprocess
import time

import repos
from review_discovery import Discovery
from review_guardian import alive, cleanup_attempt
from review_lock import acquire, pages, try_lock
from review_render import Renderer
from review_routing import load, owner
from review_store import Store, atomic, bundle_digest, delivery_id, read, unlink


def legacy_busy(data, references, work=None):
    """Observation only: unknown legacy descendants block, never get killed."""
    roots = [Path(data).resolve(), Path(references).resolve()]
    if work:
        roots.append(Path(work).resolve())
    me = os.getpid()
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal() or int(path.name) == me:
            continue
        try:
            links = [path / "cwd", path / "exe", *list((path / "fd").iterdir())]
            resolved = []
            for link in links:
                try:
                    resolved.append(link.resolve(strict=True))
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    pass
            argv = (path / "cmdline").read_bytes().decode(errors="replace")
            if (any(value.is_relative_to(root) for value in resolved for root in roots)
                    or any(str(root) + "/" in argv for root in roots)):
                return True
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return False


def pending(data, repository, deadline=None):
    prefix = "pr:" + repository.lower() + "#"
    # A crash can leave a committed journal/bundle before outbox fan-out. An
    # empty outbox alone is not a fence against its next recovery controller.
    operations = [read(p) for p in Path(data).glob("operations/*.json")]
    for operation in operations:
        if operation.get("pr", "").startswith(prefix):
            if any(not (Path(data) / "receipts" / (i["id"] + ".json")).exists()
                   for i in operation["intents"]):
                return True
    store = Store(data)
    for path in (Path(data) / "results" / repository).glob("*/current"):
        for bundle in store.chain(prefix + path.parent.name):
            if any(not (Path(data) / "receipts" / (i["id"] + ".json")).exists()
                   for i in bundle["intents"]):
                return True
    for path in Path(data).glob("runs/*/run.json"):
        config = read(path)
        directory = path.parent
        summary = read(directory / "summary.json", {})
        if summary.get("state") == "complete":
            continue
        controller = read(directory / "controller.json", summary)
        if alive(controller):
            rows = config.get("candidates", [])
            # An unfinished discovery has not yet proved which PRs it will
            # project. A paused queued controller can still write page intents;
            # it must finish/abort before ownership transfers, not just have no
            # live guardian at this instant.
            if not rows or any(c["repository"].lower() == repository for c in rows):
                return True
    for path in Path(data).glob("outbox/*.json"):
        if read(path, {}).get("pr", "").startswith(prefix):
            return True
    for path in Path(data).glob("runs/*/attempts/*/job.json"):
        if read(path, {}).get("pr", "").startswith(prefix):
            attempt = path.parent
            if any(alive(read(attempt / name, {})) for name in
                   ("status.json", "manager.json", "launch.json")):
                return True
            status = read(attempt / "status.json", {})
            if not status.get("empty"):
                limit = min(deadline or time.monotonic() + 5, time.monotonic() + 5)
                if time.monotonic() >= limit or not cleanup_attempt(attempt, limit):
                    return True
    return False


def strip_rows(raw, keys):
    """Remove only transferred manifest entries, sections and TOC rows."""
    for key in keys:
        anchor = "pr" + key.replace("#", "-")
        raw = re.sub(r'<section\b[^>]*id="' + re.escape(anchor) + r'"[^>]*>.*?</section>', '', raw, flags=re.S)
        raw = re.sub(r'<tr\b[^>]*>(?:(?!</tr>).)*href="#' + re.escape(anchor) + r'"(?:(?!</tr>).)*</tr>', '', raw, flags=re.S)
    def manifest(match):
        heads = [s for s in unescape(match[1]).split() if s.rsplit(':', 1)[0] not in keys]
        return match[0].replace(match[1], escape(' '.join(heads), quote=True))
    return re.sub(r'<!-- reviewprs-manifest v1 [^>]*heads="([^"]*)"[^>]*-->', manifest, raw)


def import_manifest(store, repository, rows, target, config):
    ticket = store.ticket()
    for pr, row in rows.items():
        if not pr.startswith("pr:" + repository + "#"):
            continue
        if not row.get("section"):
            raise ValueError("missing previous section for " + pr)
        lock = try_lock(store.locks, pr)
        if lock is None:
            raise RuntimeError("PR is still owned: " + pr)
        with lock:
            if not store.clean_owner(lock):
                raise RuntimeError("previous payload not empty")
            current = store.bundle(pr)
            if current and current["inputs"]["head"] != row["head"]:
                raise ValueError("import conflicts with accepted head: " + pr)
            if not current:
                number = int(pr.split("#")[1])
                inputs = dict(repository=repository, number=number, head=row["head"],
                              manifest_key=str(number), configuration=config)
                # Generation zero is an explicitly imported legacy review, not
                # evidence that four new inference passes ran successfully.
                bundle = dict(schema=1, legacy=True, pr=pr, generation=0,
                              run="legacy-import", request="legacy-import", inputs=inputs,
                              results={"reconciliation": {"section_md": row["section"]}},
                              previous=None, intents=[
                                  dict(kind="projection", target=target,
                                       patches={pr: dict(ticket=ticket, removed=False, generation=0)}),
                                  dict(kind="publish", target=target)], selected={})
                for intent in bundle["intents"]:
                    intent["id"] = delivery_id(pr, 0, intent["kind"], target)
                directory = store.pr_dir(pr) / "generations/0"
                atomic(directory / "bundle.json", bundle)
                for intent in bundle["intents"]:
                    store.receipt(dict(intent, pr=pr, generation=0), "imported")
                atomic(store.pr_dir(pr) / "current", dict(generation=0, digest=bundle_digest(directory)))
                current = bundle
            store.merge_membership(target, {pr: dict(ticket=ticket, removed=False,
                                                     generation=current["generation"])})


def scan_mirror(repository, mirror):
    source = mirror / "RsyncReviews/index.html"
    parser = Discovery(None, {"repos": repos.load()})
    # Bare numbers in the rsync page refer to rsync, not the main repository.
    parser.repos = {repository: dict(repo=repository, key="")}
    rows = parser.parse_manifest(source.read_text())
    for row in rows.values():
        if not row.get("section"):
            raise ValueError("manifest has a head without a previous section")
    shared = []
    for path in sorted(mirror.rglob("*.html")):
        if path.is_symlink():
            raise ValueError("publication mirror must not contain symlinks")
        if path == source:
            continue
        raw = path.read_text()
        # Old shared manifests use the repos.json key (rsync#N).
        match = re.search(r'<!-- reviewprs-manifest v1 [^>]*heads="([^"]*)"', raw)
        keys = {s.rsplit(':', 1)[0] for s in unescape(match[1]).split()
                if s.startswith('rsync#')} if match else set()
        if keys:
            shared.append((path, strip_rows(raw, keys)))
    return source, rows, shared


def handoff(root, data, repository, direction, mirror, *, dry=False, wait=300,
            publish=None, rsync_args=(), references=None):
    root, data, mirror = Path(root), Path(data), Path(mirror)
    repository = repository.lower()
    # One owner canary is deliberately limited to the designed rsync boundary.
    if repository != "rsyncproject/rsync":
        raise ValueError("only the rsync repository canary is supported")
    routing = load(root)
    if direction == "old" and (routing["modes"] or routing["labels"]):
        raise ValueError("rollback repository before transferring shared modes/labels")
    target = "page:review/RsyncReviews/index.html"
    source, rows, shared = scan_mirror(repository, mirror)
    changes = dict(repository=repository, before=owner(routing, "rsync", repository),
                   after=direction, imported_prs=len(rows),
                   scrubbed_pages=[str(p.relative_to(mirror)) for p, _ in shared])
    if dry:
        return dict(changes, dry_run=True)
    deadline = time.monotonic() + wait
    store = Store(data)
    with ExitStack() as stack:
        pause = acquire(store.locks, "pause", deadline)
        if pause is None:
            raise TimeoutError("admission already paused; finish/resume that pause first")
        stack.enter_context(pause)
        old = stack.enter_context(open(root / "etc/reviewprs.lock", "a"))
        while True:
            try:
                fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("old run did not drain")
                time.sleep(0.05)
        while True:
            # Say which fence held: an operator needs to know whether to wait
            # for a run or to drain the outbox, and a test needs to tell too.
            debts = pending(data, repository, deadline)
            busy = not debts and legacy_busy(data, references or root / "repositories", root / "work")
            if not debts and not busy:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(("delivery debts" if debts else "attempts or legacy readers")
                                   + " did not drain")
            time.sleep(0.05)
        # Reread under both admission fences. A previous successful handoff is
        # idempotent; a failed one repeats publication before committing route.
        routing = load(root)
        if publish:
            # Refresh only after draining: a mirror fetched while an old run
            # was still publishing could import an obsolete head.
            subprocess.run(["rsync", "-a", *rsync_args, "--", publish.rstrip('/') + '/', str(mirror) + '/'],
                           check=True, timeout=max(.1, deadline - time.monotonic()))
        source, rows, shared = scan_mirror(repository, mirror)
        journal = data / "handoff" / repository / "pending.json"
        previous = read(journal)
        if previous and previous["direction"] != direction:
            raise ValueError("finish the pending handoff before reversing it")
        patches = dict(previous.get("pages", {}) if previous else {})
        patches.update({p.relative_to(mirror).as_posix(): raw for p, raw in shared})
        atomic(journal, dict(direction=direction, pages=patches))
        shared = [(mirror / name, raw) for name, raw in patches.items()]
        changes.update(before=owner(routing, "rsync", repository), imported_prs=len(rows),
                       scrubbed_pages=sorted(patches))
        config = {"repos": repos.load()}
        if direction == "new":
            # The admission fence belongs to the coordinator. The leaf owns
            # PR then page, in normal order, on separate open descriptions.
            subprocess.run([sys.executable, "-c",
                "import json,sys; from review_handoff import import_manifest; "
                "from review_store import Store; a=json.load(sys.stdin); "
                "import_manifest(Store(a[0]), *a[1:])"],
                input=json.dumps([str(data), repository, rows, target, config]), text=True,
                check=True, timeout=max(.1, deadline - time.monotonic()),
                env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent)))
        else:
            with pages(store.locks, [target], deadline):
                atomic(source, Renderer(store).render(target), encode=lambda b: b)
        for path, raw in shared if direction == "new" else []:
            key = "page:review/" + path.relative_to(mirror).as_posix()
            with pages(store.locks, [key], deadline):
                backup = data / "handoff" / repository / path.relative_to(mirror)
                if not backup.exists():
                    atomic(backup, path.read_bytes(), encode=lambda b: b)
                atomic(path, raw, encode=lambda s: s.encode())
                if publish:
                    subprocess.run(["rsync", "--mkpath", *rsync_args, "--", str(path),
                                    publish.rstrip('/') + '/' + path.relative_to(mirror).as_posix()],
                                   check=True, timeout=max(.1, deadline - time.monotonic()))
        if direction == "old" and publish:
            with pages(store.locks, [target], deadline):
                subprocess.run(["rsync", "--mkpath", *rsync_args, "--", str(source),
                                publish.rstrip('/') + '/RsyncReviews/index.html'], check=True,
                               timeout=max(.1, deadline - time.monotonic()))
        owned = set(routing["repositories"])
        if direction == "new":
            owned.add(repository)
        else:
            owned.discard(repository)
        routing["repositories"] = sorted(owned)
        atomic(root / "etc/routing.json", routing)
        atomic(data / "handoff" / repository / "last.json", changes)
        unlink(journal)
    return changes

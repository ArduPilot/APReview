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


def pending(data, repository=None, deadline=None):
    """Undelivered work for one repository, or for every one when None."""
    prefix = "pr:" + (repository.lower() + "#" if repository else "")
    # A crash can leave a committed journal/bundle before outbox fan-out. An
    # empty outbox alone is not a fence against its next recovery controller.
    operations = [read(p) for p in Path(data).glob("operations/*.json")]
    for operation in operations:
        if operation.get("pr", "").startswith(prefix):
            if any(not (Path(data) / "receipts" / (i["id"] + ".json")).exists()
                   for i in operation["intents"]):
                return True
    store = Store(data)
    results = Path(data) / "results"
    for path in (results / repository).glob("*/current") if repository else results.glob("*/*/*/current"):
        pr = "pr:" + path.parent.parent.parent.name + "/" + path.parent.parent.name + "#" + path.parent.name
        for bundle in store.chain(pr):
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
            if not rows or repository is None or any(c["repository"].lower() == repository for c in rows):
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


def legacy_bundle(store, pr, row, target, ticket, config, key=None):
    """Generation zero: an explicitly imported legacy review, not evidence
    that four new inference passes ran successfully. Caller owns the PR."""
    repository, number = pr[3:].split("#")
    inputs = dict(repository=repository, number=int(number), head=row["head"],
                  manifest_key=key or number, configuration=config)
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
    return bundle


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
                current = legacy_bundle(store, pr, row, target, ticket, config)
            store.merge_membership(target, {pr: dict(ticket=ticket, removed=False,
                                                     generation=current["generation"])})


SHARED_LABELS = ("AIReview", "DevCallTopic", "DevCallEU")


def shared_pages(mirror):
    """The latest shared pages the full cutover imports: one per label, one
    per author. Dated archives and followup reports are history; the new
    path writes its own."""
    pages = [mirror / "DevCallReviews" / label / "devcall_pr_reviews.html" for label in SHARED_LABELS]
    users = mirror / "UserReviews"
    if users.is_dir():
        pages += sorted(p for p in users.glob("*.html") if p.name != "files.html")
    return [p for p in pages if p.exists()]


def scan_shared(mirror, parser):
    """Rows per shared page. Every manifest key must resolve to a configured
    repository and carry its section, or the page would lose that review on
    its first republish."""
    imports, problems = [], []
    for path in shared_pages(mirror):
        if path.is_symlink():
            raise ValueError("publication mirror must not contain symlinks")
        rel = path.relative_to(mirror).as_posix()
        parser.unparsed = []
        try:
            rows = parser.parse_manifest(path.read_text())
        except OSError as error:
            problems.append(rel + ": " + str(error))
            continue
        problems += [rel + ": unknown key " + key for key in parser.unparsed]
        problems += [rel + ": no section for " + pr for pr, row in rows.items() if not row.get("section")]
        imports.append(("page:review/" + rel, rows))
    return imports, problems


def import_pages(store, imports, config):
    """Import every shared page's rows. A PR on two pages keeps the first
    imported section; an accepted new-path generation is authoritative."""
    ticket = store.ticket()
    conflicts = []
    for target, rows in imports:
        for pr, row in rows.items():
            lock = try_lock(store.locks, pr)
            if lock is None:
                raise RuntimeError("PR is still owned: " + pr)
            with lock:
                if not store.clean_owner(lock):
                    raise RuntimeError("previous payload not empty")
                current = store.bundle(pr)
                if not current:
                    current = legacy_bundle(store, pr, row, target, ticket, config, row.get("key"))
                elif current.get("legacy") and current["inputs"]["head"] != row["head"]:
                    conflicts.append(dict(pr=pr, page=target, kept=current["inputs"]["head"],
                                          skipped=row["head"]))
                store.merge_membership(target, {pr: dict(ticket=ticket, removed=False,
                                                         generation=current["generation"])})
    return conflicts


def cutover(root, data, mirror, *, dry=False, wait=300, publish=None, rsync_args=(),
            references=None, github=None):
    """Transfer every remaining mode, label and repository at once (design
    step 4). Imports the latest shared pages so the first new run republishes
    them whole, then sets modes to all. There is no automated reverse."""
    root, data, mirror = Path(root), Path(data), Path(mirror)
    parser = Discovery(github, {"repos": repos.load()})
    problems = []
    if github is not None:
        try:
            parser.swept()      # submodule repositories, so their keys resolve
        except Exception as error:
            problems.append("submodule sweep failed: " + str(error))
    imports, scanned = scan_shared(mirror, parser)
    problems += scanned
    changes = dict(direction="new", modes=["all"], before=load(root)["modes"],
                   pages=[t for t, _ in imports],
                   imported_prs=sum(len(r) for _, r in imports), problems=problems)
    if dry:
        return dict(changes, dry_run=True)
    if problems:
        raise ValueError("cannot import: " + "; ".join(problems[:20]))
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
            debts = pending(data, None, deadline)
            busy = not debts and legacy_busy(data, references or root / "repositories", root / "work")
            if not debts and not busy:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(("delivery debts" if debts else "attempts or legacy readers")
                                   + " did not drain")
            time.sleep(0.05)
        routing = load(root)
        if publish:
            # Only after draining: an old run may have published until now.
            subprocess.run(["rsync", "-a", *rsync_args, "--", publish.rstrip('/') + '/', str(mirror) + '/'],
                           check=True, timeout=max(.1, deadline - time.monotonic()))
            imports, problems = scan_shared(mirror, parser)
            if problems:
                raise ValueError("cannot import: " + "; ".join(problems[:20]))
        config = {"repos": repos.load()}
        # The admission fence belongs to the coordinator; the leaf owns PR
        # then page, in normal order, on separate open descriptions.
        result = subprocess.run([sys.executable, "-c",
            "import json,sys; from review_handoff import import_pages; "
            "from review_store import Store; a=json.load(sys.stdin); "
            "print(json.dumps(import_pages(Store(a[0]), a[1], a[2])))"],
            input=json.dumps([str(data), imports, config]), text=True, capture_output=True,
            check=True, timeout=max(.1, deadline - time.monotonic()),
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).parent)))
        changes.update(imported_prs=sum(len(r) for _, r in imports),
                       conflicts=json.loads(result.stdout.strip().splitlines()[-1]))
        routing["modes"] = sorted(set(routing["modes"]) | {"all"})
        atomic(root / "etc/routing.json", routing)
        atomic(data / "handoff" / "full" / "last.json", changes)
    return changes


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

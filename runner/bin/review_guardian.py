"""Keep ownership until the payload is empty, independently of the controller."""
import array
import ctypes
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

from review_lock import account_slot, acquire, adopt, boot_id, permit, start_time, try_lock
from review_usage import sessions
from review_schema import FILES, IDENTITY, evidence_paths, read_result
from review_store import Store, atomic, fsync_dir, mkdir, read

SCRIPT = str(Path(__file__).with_name("review-guardian.py"))


def identity(pid=None):
    pid = pid or os.getpid()
    return {"boot": boot_id(), "pid": pid, "start": start_time(pid)}


def alive(record):
    try:
        return record["boot"] == boot_id() and start_time(record["pid"]) == record["start"] and proc(record["pid"])[0] != "Z"
    except (OSError, KeyError, ValueError, ProcessLookupError):
        return False


def proc(pid):
    words = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
    return words[0], int(words[1])


def descendants(pid):
    parents = {}
    starts = {}
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            try:
                state, parent = proc(int(path.name))
                if state != "Z":
                    parents[int(path.name)] = parent
                    starts[int(path.name)] = start_time(int(path.name))
            except (OSError, ValueError):
                pass
    found = set()
    frontier = {pid}
    while frontier:
        frontier = {child for child, parent in parents.items() if parent in frontier and child not in found}
        found.update(frontier)
    return {child: starts[child] for child in found}


def subreaper():
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")


def reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                return
        except ChildProcessError:
            return


def kill_tree(pid, deadline):
    while time.monotonic() < deadline:
        children = descendants(pid)
        if not children:
            return True
        for child, start in children.items():
            try:
                fd = os.pidfd_open(child)
                try:
                    if start_time(child) == start:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
            except (ProcessLookupError, FileNotFoundError):
                pass
        time.sleep(0.02)
    return not descendants(pid)


def cgroup_empty(path):
    try:
        return "populated 0" in (Path(path) / "cgroup.events").read_text()
    except FileNotFoundError:
        return True


def kill_cgroup(path, deadline):
    path = Path(path)
    while time.monotonic() < deadline:
        if cgroup_empty(path):
            return True
        try:
            (path / "cgroup.kill").write_text("1")
        except FileNotFoundError:
            return cgroup_empty(path)
        time.sleep(0.02)
    return cgroup_empty(path)


def cleanup_attempt(path, deadline):
    """Acquiring a freed region is not evidence that its former payload died."""
    status = read(path / "status.json", {})
    manager = read(path / "manager.json", {})
    launch_record = read(path / "launch.json", {})
    if alive(status):
        return status.get("state") == "terminal" and status.get("empty", False)
    boot = status.get("boot") or manager.get("boot") or launch_record.get("boot")
    if boot is None:
        return not (manager or launch_record)
    if boot != boot_id():
        return True
    if launch_record.get("backend") == "plain":
        if alive(manager):
            # The manager is the subreaper for a killed guardian.
            return kill_tree(manager["pid"], deadline)
        # A vanished manager with no terminal empty record needs inspection.
        return bool(status.get("empty") or read(path / "empty.json", {}).get("empty"))
    unit = launch_record.get("unit")
    if unit:
        try:
            stopped = subprocess.run(["systemctl", "--user", "stop", unit],
                                     timeout=max(0.01, deadline - time.monotonic()),
                                     capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError):
            return False
        # A transient unit that already exited is garbage-collected, and stop
        # answers "not loaded" (rc 5). That is a finished payload, not a live one.
        if stopped.returncode not in (0, 5) or (stopped.returncode == 5 and "not loaded" not in stopped.stderr):
            return False
    if status.get("cgroup"):
        return kill_cgroup(status["cgroup"], deadline)
    return bool(unit)


def memory_limits(heavy=None, total=None):
    """An attempt's CLI and its builds share one cgroup. Throttle it at its
    share of the box's memory across the build slots, and kill only past
    two shares, so one runaway cannot take the box but a large link can
    still finish. The old fixed 40G was larger than blu6's 30G."""
    heavy = heavy or int(os.environ.get("REVIEW_HEAVY_SIZE", "4"))
    if total is None:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    mib = total // (1 << 20)
    high = max(2048, int(mib * 0.8 / max(1, heavy)))
    ceiling = max(high, int(mib * 0.4))
    return ["--property=MemoryHigh=%dM" % high, "--property=MemoryMax=%dM" % ceiling]


def launch(data, attempt, pr_lock):
    attempt = Path(attempt)
    plain = os.environ.get("REVIEW_GUARDIAN_PLAIN") == "1"
    record = {"boot": boot_id(), "backend": "plain" if plain else "systemd"}
    if plain:
        atomic(attempt / "launch.json", record)
        with open(attempt / "guardian.log", "ab") as log:
            return subprocess.Popen([sys.executable, SCRIPT, "--manager", "--data", str(data),
                                     "--attempt", str(attempt), "--pr-fd", str(pr_lock.fd)],
                                    pass_fds=(pr_lock.fd,), start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log)
    unit = "review-attempt-" + uuid.uuid4().hex + ".service"
    record["unit"] = unit
    atomic(attempt / "launch.json", record)
    address = "\0review-guardian-" + uuid.uuid4().hex
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(address)
        server.listen(1)
        server.settimeout(15)
        subprocess.run(["systemd-run", "--user", "--quiet", "--unit=" + unit,
                        "--service-type=exec", "--property=Delegate=yes", "--property=KillMode=control-group",
                        *memory_limits(), "--property=TasksMax=4000", "--property=CPUQuota=1600%",
                        "--property=TimeoutStopSec=30", "--", sys.executable, SCRIPT,
                        "--data", str(data), "--attempt", str(attempt), "--socket", address[1:]],
                       check=True, timeout=20, stdout=subprocess.DEVNULL)
        with server.accept()[0] as peer:
            _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise ValueError("guardian peer uid")
            peer.sendmsg([b"P"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [pr_lock.fd]))])
    return None


def receive_fd(name):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(20)
        peer.connect("\0" + name)
        _, ancillary, _, _ = peer.recvmsg(1, socket.CMSG_SPACE(array.array("i").itemsize))
    fds = array.array("i")
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.frombytes(data)
    if len(fds) != 1:
        raise ValueError("expected one PR descriptor")
    return fds[0]


def manager(data, attempt, fd):
    """The test backend substitutes a subreaper for the user service manager."""
    subreaper()
    atomic(attempt / "manager.json", identity())
    child = subprocess.Popen([sys.executable, SCRIPT, "--data", str(data), "--attempt", str(attempt),
                              "--pr-fd", str(fd)], pass_fds=(fd,))
    os.close(fd)
    # A guardian has its own finite permit and wall deadlines.
    job = read(attempt / "job.json")
    try:
        child.wait(timeout=job.get("permit_timeout", 120) + job["wall_timeout"] + 65)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)
    empty = kill_tree(os.getpid(), time.monotonic() + 30)
    reap()
    atomic(attempt / "empty.json", {"empty": empty, **identity()})
    return 0 if empty else 1


def record_usage(status, attempt):
    observed = sessions(attempt / "payload.log")
    status["sessions"] = observed
    status["session_id"] = next(iter(observed)) if len(observed) == 1 else None
    status["usage"] = {key: sum(row.get(key, 0) for row in observed.values())
                       for key in {k for row in observed.values() for k in row}}


def run(data, attempt, fd):
    subreaper()
    store = Store(data)
    job = read(attempt / "job.json")
    pr = adopt(store.locks, job["pr"], fd)
    plain = read(attempt / "launch.json")["backend"] == "plain"
    status = {k: job[k] for k in IDENTITY}
    status.update(schema=1, **identity(), pr=job["pr"], provider=job["provider"],
                  account=job.get("account"), session_id=None, cgroup=None,
                  state="starting", heartbeat=time.time(), exit=None, timed_out=False,
                  aborted=False, result_status="missing", empty=False, slots=[], usage={}, quota={})
    atomic(attempt / "status.json", status)
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def aborted():
        return stopped or Path(job["abort_path"]).exists()
    owned = []
    child = None
    cg = None
    try:
        deadline = time.monotonic() + job.get("permit_timeout", 120)
        heartbeat = time.monotonic() + 30
        while time.monotonic() < deadline and not aborted():
            slot = permit(store.locks, job["provider"], job.get("pool_size", 8),
                          skip_finishing_slot=job["kind"] in ("primary", "cold"))
            if slot:
                if not store.clean_owner(slot):
                    slot.close()
                    raise RuntimeError("previous permit payload not empty")
                owned.append(slot)
                atomic(store.owner_path(slot), {"attempts": [str(attempt)]})
                status["slots"] = [slot.key]
                break
            if time.monotonic() >= heartbeat:
                status["heartbeat"] = time.time()
                atomic(attempt / "status.json", status)
                heartbeat = time.monotonic() + 30
            time.sleep(0.05)
        if not owned:
            status["aborted"] = aborted()
            status["error"] = "aborted" if aborted() else "permit deadline"
        else:
            if job.get("account"):
                key = "account:%s/%s" % (job["provider"], job["account"])
                account = None
                while time.monotonic() < deadline and not aborted():
                    cap = 1 if job.get("exclusive_account") else job.get("account_slots", 8)
                    account = account_slot(store.locks, key, cap,
                                           skip_finishing_slot=job["kind"] in ("primary", "cold"))
                    if account:
                        owned.append(account)
                        status["account_slot"] = account.key
                        break
                    if time.monotonic() >= heartbeat:
                        record_usage(status, attempt)
                        status["heartbeat"] = time.time()
                        atomic(attempt / "status.json", status)
                        heartbeat = time.monotonic() + 30
                    time.sleep(0.05)
                if account is None:
                    raise TimeoutError("account deadline")
                quota = acquire(store.locks, "quota", min(deadline, time.monotonic() + 5))
                if quota is None:
                    raise TimeoutError("quota state deadline")
                with quota:
                    quotas = read(store.root / "quota.json", {})
                    provider_quota = quotas.get(job["provider"], {})
                    observation = provider_quota if provider_quota.get("paused") else quotas.get(key, provider_quota)
                    status["quota"] = observation
                    if observation.get("paused"):
                        status["quota_blocked"] = True
                        raise RuntimeError("quota paused")
            if not plain:
                own_path = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
                unit_root = Path("/sys/fs/cgroup") / own_path.lstrip("/")
                control = unit_root / "guardian"
                control.mkdir(exist_ok=True)
                (control / "cgroup.procs").write_text(str(os.getpid()))
                cg = unit_root / "payload"
                cg.mkdir(exist_ok=True)
                status["cgroup"] = str(cg)
            else:
                status["cgroup"] = "plain:" + str(os.getpid())
            status.update(state="owned", heartbeat=time.time(), absolute_timeout=time.time() + job["wall_timeout"])
            atomic(attempt / "status.json", status)
            if not aborted():
                def enter():
                    os.setsid()
                    if cg:
                        (cg / "cgroup.procs").write_text(str(os.getpid()))
                # Under systemd the guardian has the user manager's environment,
                # not the supervisor's; the job says what the payload needs.
                env = dict(os.environ, **job.get("env", {}), REVIEW_JOB_DIR=str(attempt))
                with open(attempt / "payload.log", "ab") as log:
                    child = subprocess.Popen(job["command"], cwd=attempt, env=env, close_fds=True,
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, preexec_fn=enter)
                deadline = time.monotonic() + job["wall_timeout"]
                status.update(state="running", heartbeat=time.time(), payload=identity(child.pid))
                atomic(attempt / "status.json", status)
                heartbeat = time.monotonic() + 30
                while child.poll() is None:
                    if aborted():
                        status["aborted"] = True
                        break
                    if time.monotonic() >= deadline or time.time() >= status["absolute_timeout"]:
                        status["timed_out"] = True
                        break
                    if time.monotonic() >= heartbeat:
                        status["heartbeat"] = time.time()
                        atomic(attempt / "status.json", status)
                        heartbeat = time.monotonic() + 30
                    time.sleep(0.05)
            else:
                status["aborted"] = True
    except Exception as error:
        status["error"] = str(error)
    finally:
        empty = kill_cgroup(cg, time.monotonic() + 30) if cg else kill_tree(os.getpid(), time.monotonic() + 30)
        if child:
            try:
                status["exit"] = child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                empty = False
        reap()
        record_usage(status, attempt)
        status["empty"] = empty
        status["aborted"] = status["aborted"] or aborted()
        try:
            result = read_result(attempt / FILES[job["kind"]], job)
            status["result_status"] = result["status"]
            for relative in set(evidence_paths(result)):
                evidence = (attempt / relative).resolve()
                if not evidence.is_relative_to(attempt.resolve()) or not evidence.is_file():
                    raise ValueError("missing or external evidence")
                with open(evidence, "rb") as stream:
                    os.fsync(stream.fileno())
                parent = evidence.parent
                while parent.is_relative_to(attempt):
                    fsync_dir(parent)
                    if parent == attempt:
                        break
                    parent = parent.parent
            atomic(attempt / FILES[job["kind"]], result)
        except FileNotFoundError:
            status["result_status"] = "invalid" if (attempt / FILES[job["kind"]]).exists() else "missing"
        except (ValueError, TypeError, KeyError, RecursionError, OSError):
            status["result_status"] = "invalid"
        status.update(state="terminal" if empty else "cleanup_blocked", heartbeat=time.time())
        atomic(attempt / "status.json", status)
        if not empty:
            # Service-manager cleanup and the region owner record fence reuse.
            raise RuntimeError("payload cleanup blocked")
        for lock in reversed(owned):
            lock.close()
        try:
            if job.get("worktree"):
                from review_inference import cleanup
                cleanup(store, job)
        finally:
            pr.close()
    return 0

# Deterministic supervisor slice

This implements the first slice of `supervisor-design.md` revision 4. It is
opt-in and does not change the existing runners. Python 3.12 or newer and
Linux OFD locks are required. All delivery adapters are local recording
stubs; there are no GitHub, publication or inference calls.

For a local run:

```sh
REVIEW_AI_STUB=1 REVIEW_GUARDIAN_PLAIN=1 python3 runner/bin/review-supervisor.py \
    --data /data/review/core --run /data/review/core/runs/example \
    --candidates /data/review/candidates.json

REVIEW_AI_STUB=1 python3 runner/bin/review-supervisor.py \
    --data /data/review/core --resume /data/review/core/runs/example
```

The guardian backend, candidate snapshot, request identity, limits and
admission deadline are frozen in `run.json`. Resume uses those values.
`--admission`, `--wall`, `--permit-timeout` and `--pool-size` set bounded test
or run limits. Mode `pr` forces a new generation for a new request;
resuming that request does not. Other modes reuse an accepted matching head.
The caller supplies already resolved canonical repositories and endpoint
ids; discovery and alias configuration are outside this slice.

The candidate file is a JSON array. Required fields are `repository`,
`number`, `node_id`, `head`, `base`, `merge_base` and `created_at`. Heads and
timestamps should use the full commit id and the same sortable UTC format.
Optional fields include `destinations` (canonical `page:endpoint/path`
keys), `post` (default false), `classification`, `rules`, `configuration`,
`title` and `thread`. `live` supplies the candidate-refresh adapter's reply.
For tests, `stub` maps primary/cold/validation/reconciliation to a behavior
object (`sleep`, `exit`, `invalid`) or a list of behavior objects for retries.

Each attempt has a registered `job.json` before launch. The guardian's
`status.json` acknowledges ownership before launching the payload. Results
carry schema version 1 and the exact run, job, attempt, generation,
repository, node_id, number, head, base, merge_base and kind identities.
`review_schema.py` defines the closed result shapes, size bounds, finding
coverage and verdict checks. The primary, cold, validation and reconciliation
files are `review.json`, `cold.json`, `validate.json` and `final.json`.
The store additionally checks the links between the four selected passes.

Without `REVIEW_GUARDIAN_PLAIN=1`, attempts use independent `systemd-run
--user` services with delegated cgroup v2 subgroups, `KillMode=control-group`
and the design's resource limits. A Unix socket transfers the PR OFD using
SCM_RIGHTS; descriptors passed to `systemd-run` itself do not reach its unit.
The user manager must support delegation and linger must be configured by
the operator. The plain backend uses a separate subreaper manager for tests;
it handles detached descendants but supplies no CPU or memory isolation.
The plain backend deliberately blocks reuse if its manager disappeared
without evidence of an empty payload.

`locks` is never replaced. Reserved region 5 stores the file-wide `RVW\x01`
layout marker and the three configured pool sizes; owned-region headers
also carry that marker. Pool resizing probes every physical slot while
excluding new acquisitions and succeeds only when the pool is drained. This fills in
the design's unspecified location for detecting an incompatible layout
before using a previously untouched region. Header kind codes are listed in
`review_lock.HEADER_KINDS`; they are distinct from acquisition ranks.
The initializer uses a nonblocking flock only while installing/checking the
marker. Region ownership always uses OFD byte ranges.

`review-lock.py --data DIR hold KEY -- COMMAND ...` holds a region for a
bounded command (`--wait` and `--timeout` precede `hold`). At the timeout it
kills the command's whole process group and exits 124, so nothing the
command started goes on working the region after the lock is released. PR
acquisition is always a single nonblocking attempt. A busy lock exits 75.
Library callers
close their `Lock` objects; they never explicitly unlock a shared OFD.

`review_store.py` provides atomic writes, allocation, immutable bundles,
promotion, operation journals, ticketed membership merge, reconstruction and
bounded outbox drains. Physical-region owner records fence stale payloads.
Receipts outlive index entries. Its optional `crash` callback names each
acceptance, evidence, projection, receipt and lost-response boundary for
tests. The stub remote retains delivery ids and payload digests so a lost
response can be reconciled without repeating the effect. Its synchronous
local absence check is intentionally specific to the stub; a future remote
comment adapter must implement revision 4's durable reconciliation windows.

An atomic `abort.json` in a run directory stops admission and is checked by
guardians at least once per second. It does not erase accepted reviews or
delivery debt. The dashboard inputs are the versioned run `summary.json`
and attempt `status.json` files. Quota pauses can be supplied in
`quota.json`, keyed by provider or `account:provider/id`; only an explicit
successful probe in a later adapter should clear them. This slice uses the
stable account id `stub` and does not access credentials.

Tests run directly:

```sh
python3 runner/tests/test_review_lock.py
python3 runner/tests/test_review_guardian.py
python3 runner/tests/test_review_store.py
python3 runner/tests/test_review_supervisor.py
```

They use `/data/review/supervisor-tests` and finite subprocess/wait bounds.
The kill matrix exercises the plain backend. Production systemd/cgroup
integration still needs the design's on-box kill/reboot canary; a passing
plain-backend test does not establish that prerequisite. Worktree isolation,
heavy-command wrappers, credential cleanup, note jobs, rendering, real
delivery policy and remote reconciliation remain integrations beyond these
four stub job types.

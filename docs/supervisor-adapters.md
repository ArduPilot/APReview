# Supervisor adapters (slice two)

These entry points are separate from the production wrapper. No cron, routing,
account migration or production deployment is performed by this slice.
Python 3.12+, Git, gh and rsync are required; Python uses only the standard library.

Start a configured run with:

```sh
runner/bin/review-supervisor.py all --data /data/review/shadow \
  --run /data/review/shadow/runs/all-example --config config.json
```

Use `--resume /data/review/shadow/runs/all-example` with the same `--data` to
resume. `--candidates candidates.json` supplies a precommitted discovery input.
`REVIEW_AI_STUB=1` selects the deterministic payload and delivery adapters;
without it the guardian runs real CLI command lines. Tests put fake executables
on PATH and never launch an inference service. `REVIEW_GUARDIAN_PLAIN=1` selects
the process-test backend; production uses the existing systemd guardian.

A minimal real-adapter configuration has this shape. Use absolute paths and
stable account and endpoint IDs. Paths shown here are examples, not defaults.

```json
{
  "endpoint": "review",
  "endpoints": {
    "review": {
      "publish": "/data/review/staging",
      "url": "http://127.0.0.1:8000"
    }
  },
  "reference_clones": {
    "ardupilot/ardupilot": "/data/review/references/ardupilot"
  },
  "comment_accounts": ["AP-Review", "tridge"],
  "github_accounts": {
    "read": {"id": "reader", "config_dir": "/data/review/auth/gh-read"},
    "comment": {"id": "bot", "token_env": "REVIEW_BOT_TOKEN"},
    "project": {"id": "project-owner", "config_dir": "/data/review/auth/gh-project", "project": true}
  },
  "github_writes": false,
  "providers": {
    "claude": {
      "account": "claude-account",
      "home": "/data/review/auth/claude",
      "model": "configured-claude-model",
      "effort": "high",
      "permission_mode": "auto",
      "granted_directories": ["/data/review/shadow"]
    },
    "codex": {
      "account": "codex-account",
      "home": "/data/review/auth/codex",
      "model": "configured-codex-model",
      "effort": "high",
      "permission_mode": "workspace-write",
      "granted_directories": ["/data/review/shadow"]
    }
  }
}
```

Configure every swept reference clone, publication endpoint, and the project node
ID (`project_id`) before using the corresponding mode. `publish` accepts the
existing rsync remote syntax; it can alternatively come from `REVIEW_PUBLISH`.
Credential secrets are not copied into configuration. Project reads discard the
inherited comment token. Provider account leases default to exclusive until
credential concurrency has been demonstrated; `exclusive_account: false` opts
an account into shared leases. Aliases of one credential home must use the same
account ID. Set `path` to freeze the payload search path explicitly. The adapter
removes the reference checkout's autotest directory, including symlink aliases.

Run creation freezes repository configuration, prompts, dates, provider settings
and destinations. Explicit `repos`, `date`, `stamp`, `labels`, `authorize_post`,
`manifest_urls` and imported `manifests` may override discovery inputs. Published
manifests use the existing keys (`wiki`, `upstream-mavlink`, `mavlink`, and bare
main-repository PR numbers). Accepted store membership is authoritative. Existing
historical HTML is an input to discovery; populating the accepted store from the
old runner remains the slice-three migration prerequisite. Do not switch a
production page to this renderer before that import.

Discovery can run independently:

```sh
runner/bin/review-discover.py AIReview --config config.json \
  --data /data/review/shadow --record /data/review/recordings \
  --output /data/review/candidates.json
```

Use `--replay` instead of `--record` to prohibit network fallback. Each recording
is canonical JSON containing the request identity and public response, named by
SHA-256 of the request. The test fixture directory contains read-only recordings
of APReview PR #2 and its issue comments, review comments and reviews. Synthetic
fixtures cover pagination limits, multiple accounts, rebase-only patches and
changed binary blobs. No fixture records credentials.

Comment writes remain disabled unless `github_writes` is explicitly enabled in
the frozen configuration. A missing response is never treated as a failed write:
the outbox keeps exact bytes/action/account/predecessors, waits a two-minute grace
period, and records two absent reads at least 60 seconds apart before retrying.
Duplicate markers or changed bodies stop automatic retries and keep the debt
uncertain, blocking newer comments. Deprecation and board acknowledgement have
separate receipts. The legacy `post-comments.py` call path retains its default
behavior; `type: note` opts its plan entries into note-aware selection, and
`post(..., return_response=True)` exposes the returned comment JSON.

Legacy prose comments do not have structured finding IDs. Discovery conservatively
makes every prose paragraph a previous-round obligation, with stable IDs based on
comment ID and paragraph position. This avoids silently losing findings based on
wording; reconciliation must engage with each obligation. It is deliberately more
inclusive than trying to infer only headings containing “BUG” or “ISSUE”.

`review-heavy.sh COMMAND...` holds an inherited heavy permit through background
descendants. Nested wrappers reuse it. `review-heavy.sh --netns COMMAND...` uses
`netns-run.sh`; prompts require this for commands binding ports. Dependency
prefetch runs before namespace entry. Heavy wrapping is a required job contract,
not a syscall sandbox that can recognize arbitrary build commands.

`project-sync.sh --drain --data ... --config ...` performs a bounded drain without
an outer board lock. Its ordinary sweep obtains credential and board regions in
that order. Contention is exit 75; setup and write failures are nonzero. Callers
must handle those statuses rather than infer that an acknowledgement happened.

Tests and mutation logs use `/data/review/`. `test_review_adapters.py` covers
read-only record/replay, discovery and real Git history, canonical HTML and local
rsync/HTTP, durable sending and ambiguous writes, targeted board readback, fake
CLI worktrees, inherited heavy permits and all-mode phase/resume behavior.

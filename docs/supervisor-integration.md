# Supervisor integration: slice three

This slice is opt-in. No live routing, crontab, account, linger setting or
repository ownership was changed. No GitHub write was performed and nothing
under the real `~/review` was read or modified. The work is uncommitted.

## Files

New files:
- [docs/supervisor-integration.md](../docs/supervisor-integration.md)
- [runner/bin/review-admit.py](../runner/bin/review-admit.py)
- [runner/bin/review-control.py](../runner/bin/review-control.py)
- [runner/bin/review-credential.py](../runner/bin/review-credential.py)
- [runner/bin/review-handoff.py](../runner/bin/review-handoff.py)
- [runner/bin/review-outbox.sh](../runner/bin/review-outbox.sh)
- [runner/bin/review-pause.py](../runner/bin/review-pause.py)
- [runner/bin/review-probe-lease.py](../runner/bin/review-probe-lease.py)
- [runner/bin/review-resume.py](../runner/bin/review-resume.py)
- [runner/bin/review-route.py](../runner/bin/review-route.py)
- [runner/bin/review-run-config.py](../runner/bin/review-run-config.py)
- [runner/bin/review_control.py](../runner/bin/review_control.py)
- [runner/bin/review_credentials.py](../runner/bin/review_credentials.py)
- [runner/bin/review_dashboard.py](../runner/bin/review_dashboard.py)
- [runner/bin/review_handoff.py](../runner/bin/review_handoff.py)
- [runner/bin/review_routing.py](../runner/bin/review_routing.py)
- [runner/bin/review_usage.py](../runner/bin/review_usage.py)
- [runner/etc/routing.json.example](../runner/etc/routing.json.example)
- [runner/tests/test_review_integration.py](../runner/tests/test_review_integration.py)

Changed files:

- [runner/bin/review_lock.py](../runner/bin/review_lock.py)
- [runner/tests/test_review_lock.py](../runner/tests/test_review_lock.py)
- [commands/reviewprs.md](../commands/reviewprs.md)
- [docs/review-box.md](../docs/review-box.md)
- [docs/supervisor-design.md](../docs/supervisor-design.md)
- [runner/bin/claude-usage-probe.sh](../runner/bin/claude-usage-probe.sh)
- [runner/bin/make-runs-page.py](../runner/bin/make-runs-page.py)
- [runner/bin/pause-runs.sh](../runner/bin/pause-runs.sh)
- [runner/bin/publish-runs-page.sh](../runner/bin/publish-runs-page.sh)
- [runner/bin/quota.py](../runner/bin/quota.py)
- [runner/bin/reap-orphans.sh](../runner/bin/reap-orphans.sh)
- [runner/bin/review-drain.py](../runner/bin/review-drain.py)
- [runner/bin/review-env.sh](../runner/bin/review-env.sh)
- [runner/bin/review-now.sh](../runner/bin/review-now.sh)
- [runner/bin/review-supervisor.py](../runner/bin/review-supervisor.py)
- [runner/bin/review_delivery.py](../runner/bin/review_delivery.py)
- [runner/bin/review_discovery.py](../runner/bin/review_discovery.py)
- [runner/bin/review_guardian.py](../runner/bin/review_guardian.py)
- [runner/bin/review_inference.py](../runner/bin/review_inference.py)
- [runner/bin/review_render.py](../runner/bin/review_render.py)
- [runner/bin/run-reviewprs.sh](../runner/bin/run-reviewprs.sh)
- [runner/etc/crontab.reviewprs](../runner/etc/crontab.reviewprs)
- [runner/etc/local.conf.example](../runner/etc/local.conf.example)
- [runner/tests/mutate.py](../runner/tests/mutate.py)
- [runner/tests/test_review_adapters.py](../runner/tests/test_review_adapters.py)
- [runner/tests/test_review_supervisor.py](../runner/tests/test_review_supervisor.py)
- [runner/tests/test_runner_guard.py](../runner/tests/test_runner_guard.py)
- [runner/tests/test_runs_page.py](../runner/tests/test_runs_page.py)

Routing and frozen configuration are in `review_routing.py`, `review-route.py`
and `review-run-config.py`; shell entry points consult them before admission.
Lifecycle helpers cover pause, resume, durable abort, verified signalling and
record-based reaping. Credential helpers let wrapper preflight, legacy CLI
lifetimes and quota readers share guardian account leases. Dashboard helpers
read summaries and CLI session usage, and publication takes the page region.
`review_handoff.py` supplies the drained rsync transfer, legacy import, page
scrubbing, publication journal and rollback. Operations and exact commands are
in [review-box.md](review-box.md#supervisor-operations-opt-in) and
[the design migration section](supervisor-design.md#slice-three-commands-and-transfer-format).

## New tests

All new polling loops and subprocess fixtures have finite deadlines. The
following table names every new test and its assertion contract.

| Test | What it proves |
| --- | --- |
| [test_review_lock.py](../runner/tests/test_review_lock.py): `test_pool_initializer_cannot_make_an_unrelated_run_busy` | A held pool-initializer flock cannot falsely contend unrelated run/PR regions; permit claims still honor the pool resize gate. |
| [test_review_adapters.py](../runner/tests/test_review_adapters.py): `test_publication_uses_frozen_rsync_auth_options` | Publication passes the frozen password-file option as one argument, including spaces, while the local rsync/read-back still succeeds. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_abort_is_durable_under_busy_run_lock_and_checks_identity` | Abort persists while the run region is held, is idempotent, and leaves a reused/stale PID identity alive. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_account_shell_descriptors_must_reference_the_shared_inode` | Shell credential descriptors pointing at another inode are refused. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_author_routing_preserves_label_precedence` | A bare label wins over author routing; bare and explicit authors use the author destination policy. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_bad_config_never_defaults_to_old` | Unknown schemas/fields/modes, malformed arrays and noncanonical repositories fail closed. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_credential_probe_cannot_enter_a_guardian_account` | Cleanup waits/refuses while a guardian owns the account and preserves its native OAuth lock. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_dashboard_identity_session_deduplication_and_debt` | Old heartbeats do not imply death; sessions are deduplicated, PR states/debt displayed, stale identities dead, and absent identity unknown. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_dashboard_publisher_holds_page_region_through_rsync` | Both the renderer and rsync child probe the kernel page region and find it held. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_default_and_every_rsync_pr_spelling` | Missing routing is old-owned; reserved aliases, canonical/manual PR forms and URLs select the transferred rsync repository. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_handoff_dry_run_import_scrub_and_idempotent_rollback` | Both dry runs leave ownership/data unchanged; repeated real transfer imports sections/membership, scrubs shared rows, exports rollback, and retains bundles. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_handoff_fences_a_queued_controller_without_attempts` | An unfinished controller blocks transfer even with no live attempts. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_handoff_fences_unmaterialized_bundle_intents` | A committed bundle with an unreceipted intent blocks transfer even before outbox fan-out. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_handoff_fences_unmaterialized_page_journals` | An unreceipted page journal blocks transfer despite an empty outbox. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_handoff_missing_section_or_live_attempt_cannot_transfer` | Missing previous sections or live attempt identities prevent ownership changes. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_handoff_refuses_undrained_debt_and_old_jobs` | Outstanding deliveries and a held legacy lock independently prevent transfer. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_handoff_retries_failed_publication_before_route_commit` | A failed remote replacement leaves routing unchanged; retry republishes the journaled page even after local scrubbing. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_handoff_waits_for_legacy_open_files_outside_its_cwd` | A legacy process retaining a data-file descriptor blocks transfer after leaving the data directory. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_new_discovery_excludes_old_and_cross_owner_pages` | New discovery excludes old repositories and cannot partially replace an old-owned shared report. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_old_helper_filters_all_sources_including_followup` | The real helper removes transferred repositories in all/followup/author/label/PR modes; the prompt calls that helper. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_pause_fences_new_admission_while_old_run_finishes` | Pause protects new admission before the old global lock becomes available. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_pause_holds_both_paths_and_resume_verifies_identity` | A real detached pause holder owns both kernel lock domains and is resumed through its identity record. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_quota_reader_obeys_the_account_lease` | An account owned by a guardian cannot be probed by the quota reader. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_reaper_ignores_live_guardians_and_unregistered_processes` | A live identity is not cleaned even with a stale heartbeat; an unrelated process using a data cwd remains alive. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_resume_uses_frozen_config_and_refuses_transferred_ownership` | Resume leaves the frozen configuration unchanged and refuses old-owned modes or candidates. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_review_now_abort_never_waits_on_the_legacy_lock` | The actual shell abort entry point persists its request while the legacy lock is held. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_review_now_resolves_mode_after_stripping_interactive` | Interactive/dry-run flags are stripped before target routing and forwarded to the selected wrapper mode. |
| [test_review_integration.py](../runner/tests/test_review_integration.py): `test_signal_checks_identity_before_and_after_pidfd_open` | Both pre-open identity validation and the PID-reuse check after opening the pidfd are required. |
| [test_review_supervisor.py](../runner/tests/test_review_supervisor.py): `test_frozen_quota_blocks_inference_but_drains_accepted_delivery` | A quota-paused supervisor launches no attempts while receipting previously accepted publication. |
| [test_review_supervisor.py](../runner/tests/test_review_supervisor.py): `test_imported_generation_is_retained_and_next_review_updates_its_page` | The first new review follows imported generation zero and inherits its transferred report destination. |
| [test_review_supervisor.py](../runner/tests/test_review_supervisor.py): `test_paused_discovery_cannot_create_delivery_debt` | Queued/discovery projections cannot create page journals through the migration pause fence. |
| [test_review_supervisor.py](../runner/tests/test_review_supervisor.py): `test_routing_is_rechecked_before_a_new_claim` | Transferred ownership prevents both a PR claim and new page-operation journals. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_new_codex_model_must_be_pinned` | An unknown Codex model default cannot enter a supposedly frozen run. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_new_dry_run_never_creates_run` | New-path dry run validates and prints configuration without creating a run directory. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_new_manual_rsync_alias_uses_rsync_account` | Direct rsync#N wrapper invocation selects the rsync credential role. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_new_policy_exhaustion_still_starts_supervisor` | Account-policy quota exhaustion becomes admission input rather than a wrapper exit. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_new_quota_exhaustion_still_starts_supervisor` | The usage probe can exhaust quota without blocking supervisor recovery/delivery. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_new_route_skips_global_lock_and_freezes_settings` | A held legacy lock does not prevent a new launch; account/model/permission/grants/endpoints/limits are frozen and preflight account leases are released, and the legacy base clone cannot remain on the frozen PATH. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_route_is_rechecked_after_legacy_lock_wait` | A handoff during the old lock wait is detected before any CLI preflight. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_route_recheck_precedes_global_lock` | Ownership is rechecked before opening the legacy lock. |
| [test_runner_guard.py](../runner/tests/test_runner_guard.py): `test_routing_refuses_wrong_path_before_lock_or_accounts` | A caller explicitly asking for the wrong path is refused before lock/account access. |
| [test_runs_page.py](../runner/tests/test_runs_page.py): `test_supervisor_summary_replaces_wrapper_log_row` | The real HTML generator shows summary/attempt/session data and omits the duplicate wrapper-log row. |

## Existing test changes

No existing assertion was weakened or removed. `test_runner_guard.py` installs
the actual new helper dependencies and `repos.json` into its isolated home,
because routing is now a prerequisite for admission. `DiscoveryContract` in
`test_review_adapters.py` explicitly assigns its fixture to new ownership;
separate integration tests assert the production default remains old-owned.
The existing inode-based lock test and shell rationale comments remain intact.

Two new handoff rejection tests initially used a 0.1-second deadline. Removing
their guards caused the import subprocess to time out before reaching the
assertion, so those mutations were correctly reported unaccounted for, not
caught. The tests now allow one bounded second while retaining the same
rejection and unchanged-routing assertions. The incorrect handoff can finish
and produces the required assertion failure.

The full-suite baseline also exposed an existing lock-layer race: a permit
pool initializer's file-wide flock made an unrelated run region return busy,
so the existing overlapping-supervisors test observed exit 75. Initialized
non-permit regions now bypass that initializer gate; pool claims/resizing and
unknown-layout validation retain it. A deterministic kernel-lock regression
covers the failure, and the existing overlap assertion remains unchanged.

## Validation

- **620 tests in all 14 test files passed**, including all existing tests and
  42 new tests. Each file ran as its own bounded subprocess with an isolated
  home and `TMPDIR` under `/data/review/`.
- **67 added mutations, 67 caught, zero survivors/unaccounted mutations.** Each
  final catch is an assertion `FAIL` in its pinned test, not a test `ERROR`.
  Every mutation run used `REVIEW_MUTATE_DIR=/data/review/mutate`.
- The final shell syntax checks and `git diff --check` passed.
- No real inference, production canary, linger change or GitHub write was run.

Evidence retained on this development machine:

- [Full test output](/data/review/slice3-tests/final-tests.log) and
  [per-file results](/data/review/slice3-tests/final-tests.json).
- [Main 63-mutation run](/data/review/slice3-tests/mutations-final.log),
  [latest dashboard/legacy-drain guards](/data/review/slice3-tests/mutations-last.log),
  and [interactive/PATH/initializer guards](/data/review/slice3-tests/mutations-completion.log).
- [Combined validation record](/data/review/slice3-tests/validation.json) and
  [all 67 added mutation names](/data/review/slice3-tests/mutations-added.txt).

To repeat the added mutations on this machine:

```bash
REVIEW_MUTATE_DIR=/data/review/mutate python3 runner/tests/mutate.py \
  $(cat /data/review/slice3-tests/mutations-added.txt)
```

The initial two timeout-errors and the overlapping-supervisor baseline failure
are described above; neither was counted as a successful mutation catch.

## Design decisions and operational limits

- A repository is the PR ownership boundary. Labels/modes name shared report
  destinations; transferring one alone cannot admit an old-owned repository.
  `all` transfers the entire routing domain. During a repository canary,
  cross-owner shared reports are excluded rather than partially overwritten.
- Legacy reviews are imported as explicitly marked generation zero, retaining
  their original sections. They are not represented as four successful new
  passes. Imported receipts prevent duplicate delivery; inherited intents
  carry the transferred report into subsequent accepted generations.
- The handoff needs a complete publication mirror. It refreshes that mirror
  after draining, journals replacements before touching local copies, and
  commits routing only after publication succeeds. Rollback retains immutable
  bundles/receipts and does not restore stale shared-report rows. Conflicting
  accepted heads require inspection rather than silent replacement.
- The automated handoff is intentionally the rsync repository canary. The
  remaining all-at-once transfer still needs the full destination/manifest
  inventory described by the design; this slice does not infer or execute it.
- Legacy processes without attempt records are never killed by the reaper.
  The handoff conservatively waits for their cwd/executable/argv/open-file
  references to review data, work and reference clones. A timed-out transfer
  needs inspection, not a path-based kill. Queued controllers, unmaterialized
  journals/bundles and uncertain deliveries also block transfer.
- Codex must have an explicit model pin in the selected account configuration
  or override so the frozen run cannot follow a changing CLI default. Its
  configured effort/sandbox and additional writable roots are retained.
- New GitHub adapter writes remain explicitly enabled by
  `REVIEW_GITHUB_WRITES=1`; no such setting was enabled here. The real runner's
  linger and concurrent-client spikes were not run. They remain deployment
  prerequisites, with account leases exclusive until sharing is validated.

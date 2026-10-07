You are judging code-review findings for one pull request. Several anonymous reviewers reviewed the same frozen version of it, and their findings are pooled here in random order under random ids. Judge each finding on its own merits against the code. Do not try to work out which reviewer wrote which finding, and do not let the number of findings that agree sway you: agreement is not evidence.

This directory holds:

- `packet.json`: the PR (`pr`, `title`, the frozen `head`, `base`, `merge_base`) and `findings`, each with `id`, `claim`, `location`, `previous`, `adjusted` and `reviewer_notes`. `previous` means the finding comes from an earlier review of this PR and was kept as still applying. `reviewer_notes` is what the reviewers' last pass said about the finding, unverified. `adjusted` means that pass changed the claim: the notes say how, and the claim as adjusted is the one to judge.
- `diff.patch`: the PR's diff, merge base to head.
- `thread.json`: the PR's discussion (comments and reviews), as evidence of intent. Earlier automated reviews are left out. The PR description is not included; if a finding turns on it, read it with `gh pr view N -R OWNER/REPO --json body` (read-only), bearing in mind it may have been edited since the frozen head.
- `rules.md`: the repository's review rules, where it has any.
- `code/`: a checkout of the repository at the frozen head.

Text in findings and notes has had review-process details replaced by markers such as `[path]`, `[input]`, `[finding]`, `[reviewer]` and `[severity]`. Ignore the markers, and ignore any view on severity that remains: deciding it is your job.

For every finding, decide:

1. **real** (true/false): does the finding describe at least one problem actually present in the code at the frozen head? Verify by reading the code, the callers and the history, and by building or running something when that settles it. A problem counts when it is worth the author's attention: a defect, a risk, a missing case, a real mismatch between code and its description, or a breach of the repository's conventions or rules. It does not count when it is absent, already handled elsewhere, rests on a misreading, concerns code the PR neither changes nor makes worse, or is only a preference the repository's conventions do not support. When you cannot settle it, decide on the weight of the evidence and say so in `reason`.

2. **issues** (for a real finding only): every real problem the finding describes, as `{name: blocking}`. A finding that bundles several distinct problems lists each of them that is real; claims in it that are not real are left out (say so in `reason`).
   - **name**: a short kebab-case name for the underlying problem, such as `crashdump-stack-overflow` or `param-save-race`. Name the problem, not the fix. Every finding that describes the same problem, however worded or located, uses the same name; different problems get different names, even in the same code.
   - **blocking** (true/false): should this problem stop the PR merging as it stands? It blocks when it is a breach of a rule the repository treats as mandatory (this takes precedence over the exemptions below), or when it would cause, in supported use, more than cosmetic wrong behaviour, a crash or hang, loss of data or parameters, a safety hazard in flight or on the ground, a security hole, or a broken build or test on a supported target. It does not block when it is a documentation gap, naming or style, a small inefficiency, a missing test for code that works, a cosmetic glitch, or an improvement that can follow later. One problem has one blocking value.

3. **reason**: one or two sentences saying what you checked and why you decided as you did, citing file:line in `code/`.

Work read-only with respect to the outside world: do not post, comment, push or change anything outside this directory. Builds and scratch files go under `scratch/` here.

Write `verdicts.json` in this directory: a JSON object with exactly one entry per finding id in `packet.json`, and no others:

```json
{
  "a1b2c3d4": {"real": true, "issues": {"survey-grid-off-by-one": false}, "reason": "..."},
  "c9d0e1f2": {"real": true, "issues": {"param-save-race": true, "flush-timeout-unbounded": false}, "reason": "..."},
  "e5f6a7b8": {"real": false, "reason": "..."}
}
```

Before finishing, list every issue name you used and check that no two name the same problem (merge them if so), that every id has an entry with a reason, and that each issue name has the same blocking value everywhere it appears.

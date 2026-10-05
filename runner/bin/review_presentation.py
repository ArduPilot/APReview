"""How inference passes are presented their inputs: frozen per run.

A presentation is {inputs, prompts, renderer}: whether passes read job.json
or rendered files, which prompt variant, and which renderer version made
the files. A run freezes one in its configuration; claims and jobs carry it
so that input digests fence it and passes made under one presentation are
never carried into a claim under another. The legacy presentation is
recorded nowhere, so claims and reuse keys stay exactly as they were."""
from pathlib import Path

LEGACY = {"inputs": "json", "prompts": "v1", "renderer": None}
# v2-schema: still job.json, plus a schema reference, a result skeleton and
# a check command written by renderer 1, and prompts that point at them
# v3-files: inputs as files (renderer 2), prompts that read them
PRESETS = {"legacy": LEGACY,
           "v2-schema": {"inputs": "json", "prompts": "v2-schema", "renderer": 1},
           "v3-files": {"inputs": "files", "prompts": "v3-files", "renderer": 2}}
# v3-files prompts: the v1 text with every pointer into job.json replaced
FILE_POINTERS = {
    "all": [("Read job.json for pinned PR facts, diff, thread, previous findings and injected repository rules.",
             "Read inputs/README.md first, then the files it lists, for pinned PR facts, diff, thread, previous "
             "findings and repository rules; do not read job.json."),
            ("(including all identity fields from job.json)",
             "(the identity fields are filled in in inputs/result-skeleton.json)")],
    "validation": [("Challenge every primary_result finding",
                    "Challenge every finding of the primary result (inputs/results/primary.md)")],
    "reconciliation": [("fresh_snapshot title/head/thread",
                        "the PR as it is now (inputs/fresh.md and inputs/fresh-thread.md)")],
}
# every presentation this code can run, as whole combinations
KNOWN = list(PRESETS.values())
COMMANDS = Path(__file__).resolve().parents[2] / "commands"
PROMPT_FILES = {"v1": {"primary": "review-primary.md", "cold": "review-cold.md",
                       "validation": "review-validate.md", "reconciliation": "review-reconcile.md"}}


def normalise(value):
    """A frozen run's presentation, legacy when the run predates them;
    one this code cannot run is refused rather than guessed at."""
    if value is None:
        return dict(LEGACY)
    if isinstance(value, str) and value in PRESETS:
        return dict(PRESETS[value])
    if not isinstance(value, dict) or set(value) != set(LEGACY):
        raise ValueError("presentation must name exactly %s" % ", ".join(sorted(LEGACY)))
    if value not in KNOWN:
        raise ValueError("unknown presentation %r" % (value,))
    return dict(value)


def legacy(value):
    return normalise(value) == LEGACY


def prompts(variant):
    """The prompt texts of a variant, to freeze into a run."""
    texts = {kind: (COMMANDS / name).read_text() for kind, name in PROMPT_FILES["v1"].items()}
    if variant == "v3-files":
        for kind in texts:
            for old, new in FILE_POINTERS["all"] + FILE_POINTERS.get(kind, []):
                if old not in texts[kind]:
                    # a v1 prompt changed: v3 must not silently keep a job.json pointer
                    raise ValueError("v3-files prompt for %s cannot replace %r" % (kind, old))
                texts[kind] = texts[kind].replace(old, new)
    if variant in ("v2-schema", "v3-files"):
        addendum = (COMMANDS / "review-schema-addendum.md").read_text()
        texts = {kind: text.rstrip("\n") + "\n\n" + addendum for kind, text in texts.items()}
    elif variant != "v1":
        raise ValueError("unknown prompt variant %r" % variant)
    return texts


def recorded(value):
    """What a claim's inputs carry: nothing for legacy, else the presentation."""
    value = normalise(value)
    return None if value == LEGACY else value

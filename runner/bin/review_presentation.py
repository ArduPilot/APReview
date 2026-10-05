"""How inference passes are presented their inputs: frozen per run.

A presentation is {inputs, prompts, renderer}: whether passes read job.json
or rendered files, which prompt variant, and which renderer version made
the files. A run freezes one in its configuration; claims and jobs carry it
so that input digests fence it and passes made under one presentation are
never carried into a claim under another. The legacy presentation is
recorded nowhere, so claims and reuse keys stay exactly as they were."""
from pathlib import Path

LEGACY = {"inputs": "json", "prompts": "v1", "renderer": None}
# every presentation this code can run; later steps add to these
KNOWN = {"inputs": ("json",), "prompts": ("v1",), "renderer": (None,)}
COMMANDS = Path(__file__).resolve().parents[2] / "commands"
PROMPT_FILES = {"v1": {"primary": "review-primary.md", "cold": "review-cold.md",
                       "validation": "review-validate.md", "reconciliation": "review-reconcile.md"}}


def normalise(value):
    """A frozen run's presentation, legacy when the run predates them;
    one this code cannot run is refused rather than guessed at."""
    if value is None:
        return dict(LEGACY)
    if not isinstance(value, dict) or set(value) != set(LEGACY):
        raise ValueError("presentation must name exactly %s" % ", ".join(sorted(LEGACY)))
    for key, allowed in KNOWN.items():
        if value[key] not in allowed:
            raise ValueError("unknown presentation %s %r" % (key, value[key]))
    return dict(value)


def legacy(value):
    return normalise(value) == LEGACY


def prompts(variant):
    """The prompt texts of a variant, to freeze into a run."""
    return {kind: (COMMANDS / name).read_text() for kind, name in PROMPT_FILES[variant].items()}


def recorded(value):
    """What a claim's inputs carry: nothing for legacy, else the presentation."""
    value = normalise(value)
    return None if value == LEGACY else value

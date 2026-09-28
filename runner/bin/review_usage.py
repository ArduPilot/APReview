"""Session accounting from CLI protocol records, independent of inference results."""
import json


def sessions(path):
    """CLI protocol records, never the inference result's claimed resource use."""
    found, current = {}, None
    try:
        stream = open(path, errors="replace")
    except FileNotFoundError:
        return found
    with stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            sid = row.get("session_id") or row.get("thread_id")
            if sid:
                current = sid
            # Claude result usage and Codex turn.completed usage are totals
            # for their session/turn. Message events are not a second charge.
            if current and row.get("type") in ("result", "turn.completed"):
                usage = row.get("usage", {})
                if isinstance(usage, dict):
                    found[current] = {k: v for k, v in usage.items()
                                      if isinstance(v, (int, float)) and v >= 0}
    return found



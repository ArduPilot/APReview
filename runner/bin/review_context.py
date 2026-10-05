"""What filled an inference pass's context, from the CLIs' own logs.

Claude: the attempt's stream-json payload.log. Codex: every session rollout
of the thread payload.log names, under the attempt's CODEX_HOME. A request
is an observed usage-bearing model call. A measurement that cannot be made
is unknown (None) with an error, never zero; one made from a log with
damaged records keeps its figures as lower bounds but carries an error, so
reports count it as unknown."""
import glob
import json
import os
import re

PARSER = "review_context/4"
MAX_BYTES = 256 << 20           # a log larger than this is not parsed
SAVED = re.compile(r"Full output saved to:?\s*(\S+?\.txt)")
SHELLS = ("exec", "exec_command", "shell", "local_shell", "container.exec")
READERS = ("cat", "head", "tail", "sed", "less", "nl", "grep", "rg", "wc", "awk")
ORIENTING = ("pwd", "printenv", "env", "ls", "whoami", "id", "hostname", "uname")
FIELDS = ("requests", "peak_input", "sum_input", "input", "cached_input", "cache_creation",
          "output", "reasoning", "tool_output_chars")


class Damaged(Exception):
    pass


def label(command):
    """The command a shell call ran, past cd/source/export prefixes."""
    text = (command or "").strip()
    for part in re.split(r"&&|;|\n|\|\|", text):
        words = part.split()
        if not words or words[0] in ("cd", "source", ".", "export", "set", "pwd", "printenv"):
            continue
        return "Bash:" + os.path.basename(words[0])[:24]
    words = text.split()
    return "Bash:" + (os.path.basename(words[0])[:24] if words else "?")


def lines(path, damaged):
    """JSON records of a log; records that start like JSON but do not parse
    are counted in damaged[0]."""
    if os.path.getsize(path) > MAX_BYTES:
        raise ValueError("log too large: %s" % path)
    with open(path, errors="replace") as stream:
        for line in stream:
            if not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError:
                damaged[0] += 1
                continue
            if isinstance(row, dict):
                yield row


def count_ok(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def number(usage, key, damaged):
    """A usage figure: absent is 0; present but not a count is damage."""
    if key not in usage:
        return 0
    value = usage[key]
    if count_ok(value):
        return value
    damaged[0] += 1
    return 0


def check_usage(usage, damaged):
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
        number(usage, key, damaged)


def empty(provider):
    return dict(schema=1, parser=PARSER, provider=provider, error=None, retry=False, warnings=[],
                sources=[], session_id=None, cli_version=None, first_request=None, tools={},
                retries=None,           # not recorded by either CLI
                compactions=0, saved_outputs=dict(count=0, preview_chars=0, readback_reads=0, readback_chars=0),
                signals=dict(schema_source_reads=0, schema_checks=0, schema_check_failures=0, schema_repairs=0,
                             orienting_calls=0, orienting_commands=0, unreadable_commands=0),
                subagents=dict(requests=0, sum_input=0, output=0, tool_output_chars=0),
                **{k: 0 for k in FIELDS})


def add_request(out, fresh, cached, written, output, reasoning=0):
    total = fresh + cached + written
    out["requests"] += 1
    out["sum_input"] += total
    out["peak_input"] = max(out["peak_input"], total)
    out["input"] += fresh
    out["cached_input"] += cached
    out["cache_creation"] += written
    out["output"] += output
    if out["reasoning"] is not None:
        out["reasoning"] += reasoning
    if out["first_request"] is None:
        out["first_request"] = dict(input=fresh, cached=cached, cache_creation=written)


def add_tool(out, name, chars):
    tool = out["tools"].setdefault(name, dict(calls=0, chars=0))
    tool["calls"] += 1
    tool["chars"] += chars
    out["tool_output_chars"] += chars


def claude(path):
    out = empty("claude")
    out["reasoning"] = None                 # not reported by the CLI
    out["sources"].append(str(path))
    damaged, seen, calls, saved, child_calls = [0], set(), {}, set(), set()
    usage_seen = False
    for row in lines(path, damaged):
        kind = row.get("type")
        if kind == "system":
            if row.get("subtype") == "compact_boundary":
                out["compactions"] += 1
            if row.get("subtype") == "init":
                out["session_id"] = out["session_id"] or row.get("session_id")
                out["cli_version"] = out["cli_version"] or row.get("claude_code_version")
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        child = bool(row.get("parent_tool_use_id"))
        if kind == "assistant":
            usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
            ident = message.get("id")
            # one message is logged once per content block with the same usage;
            # a message without an id cannot be matched, so it counts alone
            if usage is not None and (ident is None or ident not in seen):
                usage_seen = True
                if ident is None:
                    out["warnings"].append("assistant message without id")
                else:
                    seen.add(ident)
                fresh = number(usage, "input_tokens", damaged)
                cached = number(usage, "cache_read_input_tokens", damaged)
                written = number(usage, "cache_creation_input_tokens", damaged)
                output = number(usage, "output_tokens", damaged)
                if child:
                    sub = out["subagents"]
                    sub["requests"] += 1
                    sub["sum_input"] += fresh + cached + written
                    sub["output"] += output
                else:
                    add_request(out, fresh, cached, written, output)
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    given = block.get("input") if isinstance(block.get("input"), dict) else {}
                    name = block.get("name", "?")
                    command = given.get("command") if isinstance(given.get("command"), str) else ""
                    if name == "Bash":
                        name = label(command)
                    calls[block.get("id")] = (name, given.get("file_path") if isinstance(given.get("file_path"), str) else None,
                                              command)
                    if child:
                        child_calls.add(block.get("id"))
        elif kind == "user":
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                body = block.get("content")
                text = body if isinstance(body, str) else json.dumps(body)
                ident = block.get("tool_use_id")
                name, path, command = calls.get(ident, ("?", None, ""))
                if child or ident in child_calls:
                    out["subagents"]["tool_output_chars"] += len(text)
                    continue
                add_tool(out, name, len(text))
                signal(out, name, path, command, text)
                # a read of a saved output, and a new saved output, can be the same call
                if reads_saved(name, path, command, saved):
                    out["saved_outputs"]["readback_reads"] += 1
                    out["saved_outputs"]["readback_chars"] += len(text)
                found = SAVED.search(text)
                if found and ("persisted-output" in text or "Output too large" in text):
                    saved.add(found.group(1))
                    out["saved_outputs"]["count"] += 1
                    out["saved_outputs"]["preview_chars"] += len(text)
    if not usage_seen:
        raise ValueError("no usage records")
    if damaged[0]:
        raise Damaged("%d damaged records" % damaged[0], out)
    return out


def reads_saved(name, path, command, saved):
    """A Read of a saved output's exact path, or a reader command (cat, sed,
    ...) given that exact path as an argument."""
    if not saved:
        return False
    if name == "Read":
        return path in saved
    if not name.startswith("Bash:"):
        return False
    for words in commands(command):
        if words and os.path.basename(words[0]) in READERS and any(w in saved for w in words[1:]):
            return True
    return False


def commands(text):
    """Shell commands as lists of words: quotes, backslash escapes and
    comments as the shell treats them, split at unquoted ; & | and newlines.
    Unbalanced quoting gives nothing, since it cannot be read reliably."""
    out, words, word, quoted = [], [], [], False
    i, n = 0, len(text)
    def end_word():
        nonlocal word, quoted
        if word or quoted:
            words.append("".join(word))
        word, quoted = [], False
    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] != "\n":
                word.append(text[i + 1])
            i += 2
            continue
        if c == "'":
            close = text.find("'", i + 1)
            if close < 0:
                return []
            word.append(text[i + 1:close])
            quoted = True
            i = close + 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] == "\n":
                    i += 2              # a line continuation: both go
                    continue
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
                    i += 1
                word.append(text[i])
                i += 1
            if i >= n:
                return []
            quoted = True
            i += 1
            continue
        if c == "#" and not word and not quoted:
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in " \t\r":
            end_word()
        elif c in ";&|\n":
            end_word()
            if words:
                out.append(words)
            words = []
        else:
            word.append(c)
        i += 1
    end_word()
    if words:
        out.append(words)
    return out


def checks_in(words):
    """Whether one command runs review_schema.py's check: the script run
    directly, or as the script argument of a Python interpreter, with only
    env, VAR=value and option words before it."""
    rest = list(words)
    while rest and "=" in rest[0] and not rest[0].startswith("-"):
        rest.pop(0)                         # shell assignments
    if rest and os.path.basename(rest[0]) == "env":
        rest.pop(0)
        while rest:
            word = rest[0]
            if word in ("-u", "--unset", "-C", "--chdir"):
                rest = rest[2:]             # options taking an argument
            elif word.startswith("-S") or word.startswith("--split-string"):
                return False                # a command split from one string: not followed
            elif word.startswith("-") and word != "-":
                rest.pop(0)
            elif "=" in word:
                rest.pop(0)
            else:
                break
    if rest and os.path.basename(rest[0]).startswith("python"):
        rest.pop(0)
        while rest and rest[0].startswith("-") and rest[0] != "-":
            option = rest.pop(0)
            if option.startswith("--"):
                continue
            letters = option[1:]
            for k, letter in enumerate(letters):
                if letter in "cm":
                    return False            # -c code or -m module: no script runs
                if letter in "WX":          # take an argument, attached or next
                    if k == len(letters) - 1 and rest:
                        rest.pop(0)
                    break
    return len(rest) >= 2 and rest[0].endswith("review_schema.py") and rest[1] == "check"


def output_text(text):
    """A tool's output as the command printed it: Codex can wrap it as JSON
    with an output field, alone or after a preamble line."""
    out = []
    for line in text.splitlines() or [text]:
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                value = json.loads(stripped)
            except ValueError:
                value = None
            if isinstance(value, dict) and isinstance(value.get("output"), str):
                out.append(value["output"])
                continue
        out.append(line)
    whole = text.strip()
    if whole.startswith("{") and "\n" in whole:
        try:
            value = json.loads(whole)
            if isinstance(value, dict) and isinstance(value.get("output"), str):
                return value["output"]
        except ValueError:
            pass
    return "\n".join(out)


def orienting(words):
    """A command that only looks around. env and printenv print the
    environment; env followed by a command runs that command instead."""
    first = os.path.basename(words[0])
    if first == "env":
        return all(w.startswith("-") or "=" in w for w in words[1:])
    return first in ORIENTING


def signal(out, name, path, command, text):
    """Counts step 3 of the plan watches: reading the schema validator's
    source; running its check, failing it, and checking again after a
    failure (a repair); and looking around (pwd, ls, env, ...), in calls
    that did nothing else and as commands within any call."""
    signals = out["signals"]
    parts = [words for words in (commands(command) if command else []) if words]
    if (name == "Read" and (path or "").endswith("review_schema.py")) or any(
            os.path.basename(words[0]) in READERS and any(w.endswith("review_schema.py") for w in words[1:])
            for words in parts):
        signals["schema_source_reads"] += 1
    invoked = sum(1 for words in parts if checks_in(words))
    if invoked:
        # each check that ran printed one verdict line, valid or invalid:/
        # usage:, in order; a check that printed none (skipped by &&, or
        # crashed) is not counted, and one after any failure is a repair
        verdicts = [line.strip().startswith(("invalid:", "usage:")) for line in output_text(text).splitlines()
                    if line.strip() == "valid" or line.strip().startswith(("invalid:", "usage:"))]
        for failed in verdicts[:invoked]:
            if signals["schema_check_failures"]:
                signals["schema_repairs"] += 1
            signals["schema_checks"] += 1
            signals["schema_check_failures"] += failed
    looking = [words for words in parts if orienting(words)]
    signals["orienting_commands"] += len(looking)
    if parts and len(looking) == len(parts):
        signals["orienting_calls"] += 1


def thread_ids(payload):
    """Thread ids payload.log names, whether or not a turn completed;
    damage in it is damage in the measurement."""
    found, damaged = [], [0]
    for row in lines(payload, damaged):
        ident = row.get("thread_id") if isinstance(row.get("thread_id"), str) else None
        if ident and ident not in found:
            found.append(ident)
    if damaged[0]:
        raise Damaged("%d damaged records in payload.log" % damaged[0], None)
    return found


def rollouts(home, thread):
    return sorted(glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*-%s.jsonl" % thread)))


CMD_LITERAL = re.compile(r"""cmd["']?\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`)""")


def js_string(body, quote):
    """A JavaScript string literal's text: its escapes as JSON reads them."""
    if quote == '"':
        try:
            return json.loads('"%s"' % body)
        except ValueError:
            return body
    body = body.replace("\\" + quote, quote)
    try:
        return json.loads('"%s"' % body.replace('"', '\\"'))
    except ValueError:
        return body


def exec_commands(name, given):
    """The shell text a Codex tool call ran, all of it, or None when it ran
    a shell but the command cannot be read."""
    if name not in SHELLS or not isinstance(given, str):
        return ""
    if name == "exec" and "exec_command" not in given:
        return ""                   # only polls a running session: runs no command
    texts = []
    for double, single, template in CMD_LITERAL.findall(given):
        if template and "${" in template:
            return None             # an interpolated command cannot be read
        texts.append(js_string(double, '"') if double else js_string(single, "'") if single else js_string(template, "`"))
    if not texts:
        try:
            parsed = json.loads(given)
            command = parsed.get("cmd") or parsed.get("command") if isinstance(parsed, dict) else None
            texts = [" ".join(command) if isinstance(command, list) else command] if command else []
        except ValueError:
            pass
    if not texts:
        return None
    return "\n".join(t for t in texts if isinstance(t, str))


def exec_label(name, given):
    """A Codex tool call's label: the shell command it ran, or the tool."""
    if name not in SHELLS:
        return name or "?"
    command = exec_commands(name, given)
    if not command:
        return "Bash:?"
    many = isinstance(given, str) and len(CMD_LITERAL.findall(given)) > 1
    return label(command.split("\n")[0]) + ("+" if many else "")


def codex(threads):
    """threads: each thread's rollouts, oldest first. The running total and
    the order of usage events carry across one thread's rollouts (a resume)
    and start again for the next thread."""
    out = empty("codex")
    out["subagents"] = None                  # child sessions are not linked yet
    by_id, rises, calls, damaged, compacting = {}, [], {}, [0], set()
    for paths in threads:
        order, previous = [], None           # this thread's usage events
        for path in paths:
            out["sources"].append(str(path))
            previous = parse_rollout(path, out, by_id, rises, calls, damaged, order, previous)
        # a compaction request is the response record whose next usage event
        # is the compaction itself: the total does not include it. Every
        # record keeps its own position, repeats included.
        compacting |= {ident for (what, ident), (after, _) in zip(order, order[1:])
                       if what == "U" and after == "C"}
    requests = list(by_id.values()) or rises
    if not requests:
        raise ValueError("no usage records")
    for usage in requests:
        total = number(usage, "input_tokens", damaged)          # includes cached
        cached = min(number(usage, "cached_input_tokens", damaged), total)
        add_request(out, total - cached, cached, 0, number(usage, "output_tokens", damaged),
                    number(usage, "reasoning_output_tokens", damaged))
    if damaged[0]:
        raise Damaged("%d damaged records" % damaged[0], out)
    if by_id and rises:
        ordinary = [usage for ident, usage in by_id.items() if ident not in compacting]
        if not agree(ordinary, rises):
            # which source is right cannot be told: the figures stay as
            # found, but nothing reports them as known
            raise Damaged("request sources disagree: %d usage records (%d compacting), %d total changes"
                          % (len(by_id), len(compacting), len(rises)), out)
        if compacting:
            out["warnings"].append("%d compaction requests missing from totals" % len(compacting))
    return out


def parse_rollout(path, out, by_id, rises, calls, damaged, order, previous):
    """One rollout's records into the running measurement; returns the last
    cumulative total, for the thread's next rollout."""
    for row in lines(path, damaged):
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind = payload.get("type")
        if row.get("type") == "session_meta":
            out["session_id"] = out["session_id"] or payload.get("id")
            out["cli_version"] = out["cli_version"] or payload.get("cli_version")
        if kind in ("compacted", "context_compacted") or row.get("type") == "compacted":
            out["compactions"] += 1
            order.append(("C", None))
        if row.get("type") == "token_usage_record":
            usage = payload.get("usage")
            ident = payload.get("response_id")
            if not isinstance(usage, dict):
                damaged[0] += 1
                continue
            check_usage(usage, damaged)     # every record, kept or not
            if ident is None:
                ident = "unidentified-%d" % len(by_id)
                out["warnings"].append("usage record without response id")
            order.append(("U", ident))
            by_id.setdefault(ident, usage)
        elif kind == "token_count" and isinstance(payload.get("info"), dict):
            # a request shows as a change in the cumulative total; a fall is
            # a reset, still a request; the same total again is a repeat
            info = payload["info"]
            totals = info.get("total_token_usage")
            last = info.get("last_token_usage")
            if totals is None and last is None:
                continue                    # a rate-limit-only record
            total = totals.get("total_tokens") if isinstance(totals, dict) else None
            if not count_ok(total) or not isinstance(last, dict):
                damaged[0] += 1
                continue
            check_usage(last, damaged)
            if total != previous:
                rises.append(last)
                order.append(("T", None))
            previous = total
        elif kind in ("custom_tool_call", "function_call"):
            given = payload.get("input") or payload.get("arguments")
            command = exec_commands(payload.get("name"), given)
            if command is None:
                out["signals"]["unreadable_commands"] += 1      # its signals cannot be counted
            calls[payload.get("call_id")] = (exec_label(payload.get("name"), given), command or "")
        elif kind in ("custom_tool_call_output", "function_call_output"):
            body = payload.get("output")
            if isinstance(body, list):
                blocks = [x["text"] for x in body if isinstance(x, dict) and isinstance(x.get("text"), str)]
            else:
                blocks = [body if isinstance(body, str) else json.dumps(body)]
            text = "".join(blocks)
            if payload.get("call_id") in calls:
                label, command = calls.pop(payload.get("call_id"))
                add_tool(out, label, len(text))
                # each block decoded on its own: a code-mode preamble or
                # several JSON results must not hide what the command printed
                signal(out, label, None, command, "\n".join(output_text(b) for b in blocks))
    return previous


def agree(records, rises):
    """Whether the response records (compaction requests excluded) and the
    cumulative total changes describe the same requests, one for one, in
    order, with the same figures."""
    keys = ("input_tokens", "cached_input_tokens", "output_tokens")
    return len(records) == len(rises) and all(
        a.get(k) == b.get(k) for a, b in zip(records, rises) for k in keys)


def failed(provider, error, retry, partial=None):
    out = {k: None for k in FIELDS}
    if partial:
        out = dict(partial)
    out.update(schema=1, parser=PARSER, provider=provider, error=error, retry=retry)
    return out


def measure(attempt, job):
    """context.json's contents for one attempt; never raises. retry says
    whether measuring again later could succeed (a log or rollout not yet
    there), so backfill does not re-read a log that will never change."""
    provider = job.get("provider", "?") if isinstance(job, dict) else "?"
    result = _measure(attempt, job, provider)
    # which presentation the pass had, so modes are compared apart, and
    # whether the guardian found its result valid (invalid: a rejection)
    result["presentation"] = (job.get("presentation") if isinstance(job, dict) else None) or "legacy"
    try:
        with open(os.path.join(attempt, "status.json")) as stream:
            status = json.load(stream)
        result["result_status"] = status.get("result_status") if isinstance(status, dict) else None
    except (OSError, ValueError):
        result["result_status"] = None
    return result


def _measure(attempt, job, provider):
    try:
        payload = os.path.join(attempt, "payload.log")
        if provider == "claude":
            return claude(payload)
        if provider == "codex":
            threads = thread_ids(payload)
            if not threads:
                return failed(provider, "payload.log names no thread", False)
            home = (job.get("env") or {}).get("CODEX_HOME")
            if not home:
                return failed(provider, "no CODEX_HOME recorded", False)
            found = {t: rollouts(home, t) for t in threads}
            missing = [t for t, paths in found.items() if not paths]
            if missing:
                # every thread the pass ran must be read, or it is undercounted
                return failed(provider, "no rollout for %s" % ",".join(missing), True)
            return codex([found[t] for t in threads])
        return failed(provider, "unknown provider %r" % provider, False)
    except Damaged as error:
        return failed(provider, "Damaged: %s" % error.args[0], False, error.args[1])
    except FileNotFoundError as error:
        return failed(provider, "FileNotFoundError: %s" % str(error)[:300], True)
    except Exception as error:
        return failed(provider, "%s: %s" % (type(error).__name__, str(error)[:300]), False)


def known(record):
    """A record reports can use: this schema, no error, every figure a count,
    read-back included; anything else is unknown, never zero."""
    if not (isinstance(record, dict) and record.get("schema") == 1 and not record.get("error")):
        return False
    if not all(count_ok(record.get(k)) for k in FIELDS if k != "reasoning"):
        return False
    saved = record.get("saved_outputs")
    return isinstance(saved, dict) and count_ok(saved.get("readback_chars"))


def readback(record):
    """Read-back chars of a record known() accepts."""
    return record["saved_outputs"]["readback_chars"]


def record(attempt, job):
    """Write context.json beside the attempt's other records."""
    from pathlib import Path
    from review_store import atomic, read
    result = measure(attempt, job)
    path = Path(attempt) / "context.json"
    try:
        same = read(path) == result
    except (OSError, ValueError):
        same = False
    if not same:                    # an unchanged retry leaves the attempt's age alone
        atomic(path, result)
    return result

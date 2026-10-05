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

PARSER = "review_context/6"
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
            except (ValueError, RecursionError):
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
                             orienting_calls=0, orienting_commands=0, unreadable_commands=0,
                             job_json_reads=0, job_json_read_chars=0, inputs_reads=0, inputs_read_chars=0,
                             review_env_sources=0, unattributed_checks=0, signal_errors=0),
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
            ident = message.get("id") if isinstance(message.get("id"), str) else None
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
            try:
                record_tool_uses(message, child, calls, child_calls)
            except Exception:               # a malformed record costs its signals, not the usage
                out["signals"]["signal_errors"] += 1
        elif kind == "user":
            try:
                record_tool_results(out, message, child, calls, child_calls, saved)
            except Exception:
                out["signals"]["signal_errors"] += 1
    if not usage_seen:
        raise ValueError("no usage records")
    if damaged[0]:
        raise Damaged("%d damaged records" % damaged[0], out)
    return out


def record_tool_uses(message, child, calls, child_calls):
    """Note each tool call in an assistant message, for its result."""
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            given = block.get("input") if isinstance(block.get("input"), dict) else {}
            name = block.get("name") if isinstance(block.get("name"), str) else "?"
            command = given.get("command") if isinstance(given.get("command"), str) else ""
            if name == "Bash":
                name = label(command)
            path = given.get("file_path") if isinstance(given.get("file_path"), str) else None
            calls[block.get("id")] = (name, path, command)
            if child:
                child_calls.add(block.get("id"))


def record_tool_results(out, message, child, calls, child_calls, saved):
    """Each tool result in a user message: its size, signals and saved
    outputs, kept apart for a subagent's."""
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
        safe_signal(out, name, path, command, text)
        # a read of a saved output, and a new saved output, can be the same call
        if reads_saved(name, path, command, saved):
            out["saved_outputs"]["readback_reads"] += 1
            out["saved_outputs"]["readback_chars"] += len(text)
        found = SAVED.search(text)
        if found and ("persisted-output" in text or "Output too large" in text):
            saved.add(found.group(1))
            out["saved_outputs"]["count"] += 1
            out["saved_outputs"]["preview_chars"] += len(text)


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


# options of the pattern-taking readers that consume the next word
TAKES_ARG = {"grep": set("ABCDdmf") | {"e"}, "rg": set("ABCgmtTMefErj") | {"e"}, "sed": {"e", "f", "l"},
             "awk": {"f", "v", "F"}}
LONG_TAKES_ARG = {"--regexp", "--file", "--glob", "--iglob", "--type", "--type-not", "--type-add", "--max-count",
                  "--after-context", "--before-context", "--context", "--max-columns", "--expression", "--encoding",
                  "--replace", "--include", "--exclude", "--exclude-dir", "--max-depth", "--sort", "--sortr",
                  "--threads", "--field-match-separator", "--path-separator"}
# long options whose argument is required for one program but optional (=value only) for another
LONG_TAKES_ARG_BY = {"rg": {"--color", "--colors"}}
GIVES_PATTERN = {"e", "f"}


def reader_files(program, args):
    """The files a reader command reads: its non-option arguments, less the
    pattern or script grep, rg, awk and sed take first unless an option
    (-e, -f, attached or not) supplies it, and less option arguments."""
    files, pattern_given, k = [], False, 0
    takes = TAKES_ARG.get(program, set())
    while k < len(args):
        word = args[k]
        k += 1
        if word == "--":
            files += args[k:]               # the rest are operands, whatever they look like
            break
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if name in ("--regexp", "--file", "--expression"):
                pattern_given = True
            if (name in LONG_TAKES_ARG or name in LONG_TAKES_ARG_BY.get(program, ())) and "=" not in word:
                k += 1
            continue
        if word.startswith("-") and len(word) > 1:
            for n, letter in enumerate(word[1:], 1):
                if letter in takes:
                    if letter in GIVES_PATTERN and program in ("grep", "rg", "sed", "awk"):
                        pattern_given = True
                    if n == len(word) - 1:
                        k += 1              # its argument is the next word
                    break                   # the rest of the cluster is its argument
            continue
        files.append(word)
    if program in ("grep", "rg", "sed", "awk") and not pattern_given and files:
        files = files[1:]
    return files


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


def safe_signal(out, *args, **kwargs):
    """signal(), but an output it cannot read costs that output's signals,
    never the pass's usage figures."""
    try:
        signal(out, *args, **kwargs)
    except Exception:
        out["signals"]["signal_errors"] += 1


def signal(out, name, path, command, text, verdicts=None):
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
    if verdicts is None:
        # one shell (a Claude Bash call): each check that ran printed one
        # verdict line, in order; one that printed none (skipped by &&, or
        # crashed) is not counted
        invoked = sum(1 for words in parts if checks_in(words))
        verdicts = verdict_lines(output_text(text))[:invoked] if invoked else []
    if verdicts:
        # a check after any failure is a repair
        for failed in verdicts:
            if signals["schema_check_failures"]:
                signals["schema_repairs"] += 1
            signals["schema_checks"] += 1
            signals["schema_check_failures"] += failed
    # where it read its inputs from: job.json, or the rendered inputs/. A
    # read is a Read of the file, a reader command given it, or a Python
    # one-liner that opens job.json; each file read counts, and the call's
    # output is attributed to what it read
    read = [path] if name == "Read" and path else []
    for words in parts:
        program = os.path.basename(words[0])
        if program in READERS:
            read += reader_files(program, words[1:])
        elif program.startswith("python") and any(
                "job.json" in w and re.search(r"open\(|read_text\(|json\.load\(", w) for w in words[1:]):
            read.append("job.json")         # a one-liner that opens it, not one that names it
    job_reads = [r for r in read if os.path.basename(r) == "job.json"]
    input_reads = [r for r in read if re.search(r"(^|/)inputs/", r)]
    if job_reads:
        signals["job_json_reads"] += len(job_reads)
        signals["job_json_read_chars"] += len(text)
    if input_reads:
        signals["inputs_reads"] += len(input_reads)
        signals["inputs_read_chars"] += len(text)
    signals["review_env_sources"] += sum(
        1 for words in parts if words[0] in ("source", ".") and any(w.endswith("review-env.sh") for w in words[1:]))
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


CMD_VALUE = re.compile(r"""\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`)""")
AFTER_VALUE = re.compile(r"\s*(?:/\*.*?\*/\s*|//[^\n]*\n\s*)*", re.S)


def exec_steps(given):
    """A Codex code-mode script's tool calls in order: ("exec", command or
    None) for each exec_command, None when its cmd is not a plain literal
    (a variable, interpolated, or joined with +, comments allowed between);
    ("poll", session id or None) for each write_stdin."""
    steps = []
    for call in re.finditer(r"\b(exec_command|write_stdin)\s*\(", given):
        kind = "poll" if call.group(1) == "write_stdin" else "exec"
        opening = re.match(r"\s*\{", given[call.end():])
        if not opening:
            steps.append((kind, None))      # its argument is not a literal object: unknown
            continue
        rest = given[call.end() + opening.end():]
        if kind == "poll":
            session = re.match(r"""[^}]*?["']?\bsession_id["']?\s*:\s*(\d+)""", rest)
            # the number must be the whole value, as a cmd literal must
            whole = session and rest[AFTER_VALUE.match(rest, session.end()).end():][:1] in (",", "}")
            steps.append(("poll", int(session.group(1)) if whole else None))
            continue
        key = re.match(r"[^}]*?\bcmd[\"']?\s*:", rest)
        value = CMD_VALUE.match(rest, key.end()) if key else None
        if not value:
            steps.append(("exec", None))
            continue
        double, single, template = value.groups()
        # a literal only when it is the whole value: next comes , or }
        whole = rest[AFTER_VALUE.match(rest, value.end()).end():][:1] in (",", "}")
        if not whole or (template is not None and "${" in template):
            steps.append(("exec", None))
        else:
            steps.append(("exec", js_string(double, '"') if double is not None else js_string(single, "'")
                          if single is not None else js_string(template, "`")))
    return steps


def exec_parse(name, given):
    """A Codex shell call's commands in order (None for one that cannot be
    read), or None when it ran a shell but no command can be found."""
    if name not in SHELLS or not isinstance(given, str):
        return []
    if name == "exec":
        if "exec_command" not in given:
            return []               # only polls a running session: runs no command
        return [c for kind, c in exec_steps(given) if kind == "exec"] or None
    found = CMD_LITERAL.search(given)
    if found:
        double, single, template = found.groups()
        return [js_string(double, '"') if double is not None else js_string(single, "'")
                if single is not None else js_string(template, "`")]
    try:
        parsed = json.loads(given)
        command = parsed.get("cmd") or parsed.get("command") if isinstance(parsed, dict) else None
        if isinstance(command, list):
            command = " ".join(command) if all(isinstance(w, str) for w in command) else None
        return [command] if isinstance(command, str) and command else None
    except (ValueError, RecursionError):
        return None


def result_objects(blocks):
    """The result objects a code-mode call printed, in order, decoded with
    string-aware JSON boundaries."""
    decoder, found = json.JSONDecoder(), []
    for block in blocks:
        k = block.find("{")
        while k >= 0:
            try:
                value, end = decoder.raw_decode(block, k)
            except (ValueError, RecursionError):
                k = block.find("{", k + 1)
                continue
            if isinstance(value, dict) and ("session_id" in value or "exit_code" in value):
                found.append(value)
            k = block.find("{", end)
    return found


def verdict_lines(text):
    """The check's verdict lines in text, in order: True for a failure."""
    return [line.strip().startswith(("invalid:", "usage:")) for line in text.splitlines()
            if line.strip() == "valid" or line.strip().startswith(("invalid:", "usage:"))]


def exec_commands(name, given):
    """The readable shell text a Codex tool call ran, or None when it ran a
    shell but none of its commands can be read."""
    parsed = exec_parse(name, given)
    if parsed is None:
        return None
    readable = [c for c in parsed if isinstance(c, str)]
    if parsed and not readable:
        return None
    return "\n".join(readable)


def exec_unreadable(name, given):
    parsed = exec_parse(name, given)
    return 1 if parsed is None else sum(1 for c in parsed if c is None)


def exec_label(name, given):
    """A Codex tool call's label: the shell command it ran, or the tool."""
    if name not in SHELLS:
        return name or "?"
    command = exec_commands(name, given)
    if not command:
        return "Bash:?"
    many = len(exec_parse(name, given) or []) > 1
    return label(command.split("\n")[0]) + ("+" if many else "")


def codex(threads):
    """threads: each thread's rollouts, oldest first. The running total and
    the order of usage events carry across one thread's rollouts (a resume)
    and start again for the next thread."""
    out = empty("codex")
    out["subagents"] = None                  # child sessions are not linked yet
    by_id, rises, calls, damaged, compacting = {}, [], {}, [0], set()
    for paths in threads:
        order, previous, sessions = [], None, {}      # this thread's usage events and running commands
        for path in paths:
            out["sources"].append(str(path))
            previous = parse_rollout(path, out, by_id, rises, calls, damaged, order, previous, sessions)
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


def parse_rollout(path, out, by_id, rises, calls, damaged, order, previous, sessions=None):
    """One rollout's records into the running measurement; returns the last
    cumulative total, for the thread's next rollout."""
    sessions = {} if sessions is None else sessions
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
            ident = payload.get("response_id") if isinstance(payload.get("response_id"), str) else None
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
        elif kind in ("custom_tool_call", "function_call", "custom_tool_call_output", "function_call_output"):
            try:
                codex_tool_record(out, kind, payload, calls, sessions)
            except Exception:               # a malformed record costs its signals, not the usage
                out["signals"]["signal_errors"] += 1
    return previous


def codex_tool_record(out, kind, payload, calls, sessions):
    """One Codex tool call or output record into the measurement."""
    if kind in ("custom_tool_call", "function_call"):
        name, given = payload.get("name"), payload.get("input") or payload.get("arguments")
        if name == "exec" and isinstance(given, str):
            steps = exec_steps(given)
        elif name in SHELLS:
            steps = [("exec", c) for c in (exec_parse(name, given) or [None])]
        else:
            steps = []
        tool = exec_label(name, given)
        out["signals"]["unreadable_commands"] += sum(1 for k, c in steps if k == "exec" and c is None)
        calls[payload.get("call_id")] = (tool, steps)
        return
    body = payload.get("output")
    if isinstance(body, list):
        blocks = [x["text"] for x in body if isinstance(x, dict) and isinstance(x.get("text"), str)]
    else:
        blocks = [body if isinstance(body, str) else json.dumps(body)]
    text = "".join(blocks)
    if payload.get("call_id") in calls:
        tool, steps = calls.pop(payload.get("call_id"))
        add_tool(out, tool, len(text))
        command = "\n".join(c for k, c in steps if k == "exec" and isinstance(c, str))
        signal(out, tool, None, command, "\n".join(output_text(b) for b in blocks),
               verdicts=codex_verdicts(out, steps, blocks, sessions))


def checks_of(command):
    return sum(1 for words in commands(command or "") if words and checks_in(words))


def codex_verdicts(out, steps, blocks, sessions):
    """The schema-check verdicts one Codex call produced. Attributed only
    when the call ran a single command or polled a single session: a batch
    can print its results in completion order, so whose output is whose
    cannot be told, and its checks are counted as unattributed. A running
    command's session keeps its command and the verdicts already counted,
    so a verdict printed mid-run is counted once, whichever chunk has it."""
    if len(steps) != 1:
        unattributed = sum(checks_of(c) for k, c in steps if k == "exec")
        # a session polled ambiguously is attributed no more: its checks not
        # yet counted are unattributed, once
        # and a poll whose session cannot be read could be any of them
        polled = list(sessions) if any(k == "poll" and c is None for k, c in steps) else \
            list(dict.fromkeys(c for k, c in steps if k == "poll" and c in sessions))
        for c in polled:
            state = sessions.pop(c)
            unattributed += max(0, checks_of(state["command"]) - state["counted"])
        out["signals"]["unattributed_checks"] += unattributed
        return []
    kind, value = steps[0]
    results = result_objects(blocks)
    if kind == "exec":
        if not value:
            return []
        state = {"command": value, "counted": 0}
        if results and isinstance(results[0].get("session_id"), int) and "exit_code" not in results[0]:
            sessions[results[0]["session_id"]] = state
    else:
        state = sessions.get(value)
        if not state:
            return []
    expected = checks_of(state["command"])
    if not expected:
        return []
    if results:
        output = results[0].get("output") if isinstance(results[0].get("output"), str) else ""
    else:
        output = output_text("".join(blocks))
    fresh = verdict_lines(output)[:expected - state["counted"]]
    state["counted"] += len(fresh)
    return fresh

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
    # the rendered inputs' size on disk, the cost step 4 adds per attempt
    inputs = os.path.join(attempt, "inputs")
    result["inputs_bytes"] = sum(os.path.getsize(os.path.join(d, f)) for d, _, names in os.walk(inputs)
                                 for f in names) if os.path.isdir(inputs) else 0
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

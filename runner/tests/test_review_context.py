import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import review_context as rc  # noqa: E402

BASE = os.path.join(os.environ.get("REVIEW_TEST_DIR", "/data/review"), "supervisor-tests")
os.makedirs(BASE, exist_ok=True)


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for row in rows:
            f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


def usage(fresh, cached, written, out=10):
    return {"input_tokens": fresh, "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": written, "output_tokens": out}


SAVED_PATH = "/home/x/.claude/projects/p/s/tool-results/b1.txt"
CLAUDE = [
    "Cloning into 'wt/modules/ChibiOS'...",
    {"type": "system", "subtype": "init", "session_id": "S1"},
    # one message, logged once per content block with the same usage
    {"type": "assistant", "message": {"id": "m1", "usage": usage(2, 11919, 20000), "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash",
         "input": {"command": "cd /a && source ~/review/bin/review-env.sh && python3 -c 'x'"}}]}},
    {"type": "assistant", "message": {"id": "m1", "usage": usage(2, 11919, 20000), "content": [
        {"type": "text", "text": "thinking"}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content":
        "<persisted-output>\nOutput too large (113.1KB). Full output saved to: %s\n\nPreview (first 2KB):\nabc"
        % SAVED_PATH}]}},
    {"type": "assistant", "message": {"id": "m2", "usage": usage(3, 31919, 2000), "content": [
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": SAVED_PATH}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2",
                                              "content": [{"type": "text", "text": "x" * 5000}]}]}},
    # a subagent's request is reported apart, not added to the pass
    {"type": "assistant", "parent_tool_use_id": "t9", "message": {"id": "m3", "usage": usage(5, 100, 50, 7),
                                                                  "content": []}},
    {"type": "system", "subtype": "compact_boundary"},
    {"type": "result", "usage": usage(5, 43838, 22000)},
]


class Claude(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="context-", dir=BASE))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def test_requests_tools_and_saved_outputs(self):
        write(self.dir / "payload.log", CLAUDE)
        m = rc.measure(self.dir, {"provider": "claude"})
        self.assertIsNone(m["error"])
        self.assertEqual(m["requests"], 2)                    # m1 once, the subagent apart
        self.assertEqual(m["sum_input"], (2 + 11919 + 20000) + (3 + 31919 + 2000))
        self.assertEqual(m["peak_input"], 3 + 31919 + 2000)
        self.assertEqual(m["first_request"], dict(input=2, cached=11919, cache_creation=20000))
        self.assertEqual(m["subagents"], dict(requests=1, sum_input=155, output=7, tool_output_chars=0))
        self.assertEqual(m["compactions"], 1)
        self.assertEqual(m["session_id"], "S1")
        self.assertEqual(set(m["tools"]), {"Bash:python3", "Read"})
        self.assertEqual(m["saved_outputs"]["count"], 1)
        self.assertEqual(m["saved_outputs"]["readback_reads"], 1)
        self.assertGreater(m["saved_outputs"]["readback_chars"], 5000)

    def test_a_missing_or_empty_log_is_unknown_not_zero(self):
        m = rc.measure(self.dir, {"provider": "claude"})
        self.assertIsNone(m["requests"])
        self.assertIn("FileNotFoundError", m["error"])
        self.assertTrue(m["retry"])
        write(self.dir / "payload.log", ["not json", {"type": "system", "subtype": "init"}])
        m = rc.measure(self.dir, {"provider": "claude"})
        self.assertIsNone(m["sum_input"])
        self.assertIn("no usage records", m["error"])

    def test_a_truncated_log_keeps_its_figures_but_is_not_known(self):
        write(self.dir / "payload.log", CLAUDE[:3] + ['{"type": "assistant", "message": {"id": "m9", "usa'])
        m = rc.measure(self.dir, {"provider": "claude"})
        self.assertEqual(m["requests"], 1)                    # a lower bound
        self.assertIn("damaged", m["error"])
        self.assertFalse(m["retry"])                          # the log will not change
        self.assertFalse(rc.known(m))

    def test_messages_without_ids_each_count(self):
        rows = [{"type": "assistant", "message": {"usage": usage(10, 0, 0), "content": []}},
                {"type": "assistant", "message": {"usage": usage(20, 0, 0), "content": []}}]
        write(self.dir / "payload.log", rows)
        m = rc.measure(self.dir, {"provider": "claude"})
        self.assertEqual((m["requests"], m["sum_input"]), (2, 30))
        self.assertTrue(m["warnings"])

    def test_a_subagents_tools_stay_out_of_the_pass(self):
        rows = CLAUDE[:3] + [
            {"type": "assistant", "parent_tool_use_id": "t9", "message": {"id": "c1", "usage": usage(1, 0, 0),
             "content": [{"type": "tool_use", "id": "ct", "name": "Read", "input": {"file_path": "/x"}}]}},
            {"type": "user", "parent_tool_use_id": "t9", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "ct", "content": "q" * 700}]}}]
        write(self.dir / "payload.log", rows)
        m = rc.measure(self.dir, {"provider": "claude"})
        self.assertNotIn("Read", m["tools"])
        self.assertEqual(m["subagents"]["tool_output_chars"], 700)

    def test_read_back_needs_a_reader_and_the_exact_path(self):
        saved = {SAVED_PATH}
        self.assertTrue(rc.reads_saved("Read", SAVED_PATH, "", saved))
        self.assertTrue(rc.reads_saved("Bash:cat", None, "cd /x && cat '%s'" % SAVED_PATH, saved))
        self.assertFalse(rc.reads_saved("Write", SAVED_PATH, "", saved))
        self.assertFalse(rc.reads_saved("Bash:cat", None, "cat %s.bak" % SAVED_PATH, saved))
        self.assertFalse(rc.reads_saved("Bash:rg", None, "rg -l \"%s\" job.json" % "b1.txt", saved))
        # quoting is shell quoting: another file name, or text that only mentions a command
        self.assertFalse(rc.reads_saved("Bash:cat", None, "cat '%s suffix'" % SAVED_PATH, saved))
        self.assertFalse(rc.reads_saved("Bash:printf", None, "printf '%%s' 'hello; cat %s'" % SAVED_PATH, saved))
        self.assertFalse(rc.reads_saved("Bash:cat", None, "cat '%s" % SAVED_PATH, saved))
        self.assertFalse(rc.reads_saved("Bash:cat", None, 'cat "%s".bak' % SAVED_PATH, saved))
        self.assertFalse(rc.reads_saved("Bash:printf", None, "printf x\\; cat %s" % SAVED_PATH, saved))
        self.assertTrue(rc.reads_saved("Bash:pwd", None, "pwd # where\ncat %s" % SAVED_PATH, saved))
        cut = SAVED_PATH.replace("tool-results", "tool-\\\nresults")         # continued inside double quotes
        self.assertTrue(rc.reads_saved("Bash:cat", None, 'cat "%s"' % cut, saved))
        self.assertFalse(rc.reads_saved("Bash:printf", None, "printf '%%s\\n' ';' cat %s" % SAVED_PATH, saved))
        self.assertTrue(rc.reads_saved("Bash:cat", None, "pwd\ncat %s" % SAVED_PATH, saved))

    def test_reading_a_saved_output_that_is_saved_again_counts_both(self):
        second = "/home/x/.claude/projects/p/s/tool-results/b2.txt"
        rows = CLAUDE[:5] + [
            {"type": "assistant", "message": {"id": "m2", "usage": usage(3, 1, 1), "content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cat " + SAVED_PATH}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content":
                "<persisted-output>\nOutput too large. Full output saved to: %s\n\nPreview:\n" % second}]}}]
        write(self.dir / "payload.log", rows)
        m = rc.measure(self.dir, {"provider": "claude"})
        self.assertEqual(m["saved_outputs"]["count"], 2)
        self.assertEqual(m["saved_outputs"]["readback_reads"], 1)

    def test_an_oversized_log_is_not_parsed(self):
        write(self.dir / "payload.log", CLAUDE)
        old, rc.MAX_BYTES = rc.MAX_BYTES, 10
        try:
            m = rc.measure(self.dir, {"provider": "claude"})
        finally:
            rc.MAX_BYTES = old
        self.assertIn("too large", m["error"])


def codex_rows(records):
    rows = [{"type": "session_meta", "payload": {"id": "T1", "cwd": "/a/wt"}}]
    rows += records
    return rows


def tur(ident, fresh, cached, out=5):
    return {"type": "token_usage_record", "payload": {"response_id": ident, "usage": {
        "input_tokens": fresh, "cached_input_tokens": cached, "output_tokens": out,
        "reasoning_output_tokens": 1}}}


def count(total, fresh, cached):
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": total},
        "last_token_usage": {"input_tokens": fresh, "cached_input_tokens": cached, "output_tokens": 5}}}}


EXEC = [
    {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
     "input": 'text(await tools.exec_command({cmd:"pwd; printenv REVIEW_JOB_DIR; env"}))'}},
    {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c1",
     "output": [{"type": "input_text", "text": "Script completed\n"}, {"type": "input_text", "text": "y" * 4000}]}},
    {"type": "response_item", "payload": {"type": "function_call", "call_id": "c2", "name": "shell",
     "arguments": json.dumps({"cmd": "sed -n '1,9p' diff.txt"})}},
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c2", "output": "z" * 300}},
]


class Codex(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="context-", dir=BASE))
        self.home = self.dir / "home"
        self.rollout = self.home / "sessions/2026/10/05/rollout-2026-10-05T07-05-07-T1.jsonl"
        write(self.dir / "payload.log", [{"type": "thread.started", "thread_id": "T1"},
                                         {"type": "turn.completed", "usage": {"input_tokens": 1}}])
        self.job = {"provider": "codex", "env": {"CODEX_HOME": str(self.home)}}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def test_newer_records_are_counted_once_per_response(self):
        # as the CLI writes them: a usage record and a total change per response
        write(self.rollout, codex_rows([tur("r1", 20740, 12288), tur("r1", 20740, 12288),
                                        count(20868, 20740, 12288), count(20868, 20740, 12288),
                                        tur("r2", 31514, 20608), count(52547, 31514, 20608)] + EXEC))
        m = rc.measure(self.dir, self.job)
        self.assertIsNone(m["error"])
        self.assertEqual(m["requests"], 2)                    # the repeated r1 and the token_count ignored
        self.assertEqual(m["sum_input"], 20740 + 31514)
        self.assertEqual(m["cached_input"], 12288 + 20608)
        self.assertEqual(m["input"], (20740 - 12288) + (31514 - 20608))
        self.assertEqual(m["first_request"], dict(input=8452, cached=12288, cache_creation=0))
        self.assertEqual(m["tools"]["Bash:env"]["chars"], len("Script completed\n") + 4000)
        self.assertEqual(m["tools"]["Bash:sed"]["chars"], 300)

    def test_older_records_count_a_change_in_the_total_not_a_repeat(self):
        # a fall in the total is a reset (a resume), and still a request
        write(self.rollout, codex_rows([count(100, 90, 0), count(100, 90, 0), count(250, 140, 80),
                                        count(250, 140, 80), count(30, 20, 0)]))
        m = rc.measure(self.dir, self.job)
        self.assertEqual(m["requests"], 3)
        self.assertEqual(m["sum_input"], 90 + 140 + 20)

    def test_disagreeing_request_sources_make_the_measurement_unknown(self):
        write(self.rollout, codex_rows([tur("r1", 10, 0), count(15, 10, 0), count(40, 20, 0)]))
        m = rc.measure(self.dir, self.job)
        self.assertIn("disagree", m["error"])
        self.assertFalse(rc.known(m))

    def test_null_usage_or_payload_damage_is_not_a_zero(self):
        write(self.rollout, codex_rows([{"type": "token_usage_record", "payload": {
            "response_id": "r1", "usage": {"input_tokens": None, "cached_input_tokens": 0}}}]))
        self.assertIn("damaged", rc.measure(self.dir, self.job)["error"])
        write(self.rollout, codex_rows([tur("r1", 10, 0)]))
        write(self.dir / "payload.log", [{"type": "thread.started", "thread_id": "T1"}, '{"thread_id": "T2", "tr'])
        self.assertIn("damaged", rc.measure(self.dir, self.job)["error"])

    def test_a_damaged_total_or_a_dropped_duplicate_is_still_damage(self):
        write(self.rollout, codex_rows([count(100, 90, 0), {"type": "event_msg", "payload": {"type": "token_count",
            "info": {"total_token_usage": {"total_tokens": None}, "last_token_usage": {"input_tokens": 5}}}}]))
        self.assertIn("damaged", rc.measure(self.dir, self.job)["error"])
        bad = {"type": "token_usage_record", "payload": {"response_id": "r1", "usage": {"input_tokens": -4}}}
        write(self.rollout, codex_rows([tur("r1", 10, 0), bad]))      # the duplicate is dropped, its damage is not
        self.assertIn("damaged", rc.measure(self.dir, self.job)["error"])
        write(self.rollout, codex_rows([tur("r1", 10, 0), {"type": "token_usage_record",
                                        "payload": {"response_id": "r2", "usage": "broken"}}]))
        self.assertIn("damaged", rc.measure(self.dir, self.job)["error"])
        # the two sources agree in count but not in figures
        write(self.rollout, codex_rows([tur("r1", 10, 0), count(15, 20, 0)]))
        self.assertIn("disagree", rc.measure(self.dir, self.job)["error"])
        # a token_count with no usage (rate limits only) is not damage
        write(self.rollout, codex_rows([tur("r1", 10, 0), count(15, 10, 0),
                                        {"type": "event_msg", "payload": {"type": "token_count", "info": {}}}]))
        self.assertIsNone(rc.measure(self.dir, self.job)["error"])

    def test_every_thread_must_have_its_rollout(self):
        write(self.rollout, codex_rows([tur("r1", 10, 0)]))
        write(self.dir / "payload.log", [{"type": "thread.started", "thread_id": "T1"},
                                         {"type": "thread.started", "thread_id": "T2"}])
        m = rc.measure(self.dir, self.job)
        self.assertIn("no rollout for T2", m["error"])
        self.assertTrue(m["retry"])

    def test_a_resumed_thread_reads_every_rollout(self):
        write(self.rollout, codex_rows([tur("r1", 10, 0)]))
        write(self.home / "sessions/2026/10/06/rollout-2026-10-06T01-00-00-T1.jsonl", codex_rows([tur("r2", 30, 5)]))
        m = rc.measure(self.dir, self.job)
        self.assertEqual((m["requests"], m["sum_input"]), (2, 40))
        self.assertEqual(len(m["sources"]), 2)

    def test_a_failed_turn_still_finds_its_thread(self):
        write(self.dir / "payload.log", [{"type": "thread.started", "thread_id": "T1"}, {"type": "turn.failed"}])
        write(self.rollout, codex_rows([tur("r1", 10, 0)]))
        self.assertEqual(rc.measure(self.dir, self.job)["requests"], 1)

    def test_tool_labels_follow_the_tool_and_batched_commands(self):
        calls = [
            {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "w", "name": "web_search",
                                                  "input": "query"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "w", "output": "r"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "m", "name": "exec",
             "input": 'await tools.exec_command({cmd:"cat a"}); await tools.exec_command({cmd:"sed -n 1p b"})'}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "m", "output": "ab"}}]
        write(self.rollout, codex_rows([tur("r1", 10, 0)] + calls))
        m = rc.measure(self.dir, self.job)
        self.assertEqual(set(m["tools"]), {"web_search", "Bash:cat+"})
        self.assertIsNone(m["subagents"])                     # not measured for Codex

    def test_a_missing_rollout_or_home_is_unknown(self):
        m = rc.measure(self.dir, self.job)
        self.assertIn("no rollout", m["error"])
        self.assertTrue(m["retry"])                           # it may yet be written
        m = rc.measure(self.dir, {"provider": "codex", "env": {}})
        self.assertIn("CODEX_HOME", m["error"])
        self.assertIsNone(m["requests"])
        self.assertFalse(rc.known(m))


class Baseline(unittest.TestCase):
    def test_nothing_known_is_unknown_not_zero(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("baseline", str(Path(rc.__file__).parent / "review-baseline.py"))
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
        summary = baseline.context_summary([None, {"schema": 1, "error": "x"}])
        self.assertEqual((summary["passes"], summary["unknown"]), (2, 2))
        self.assertIsNone(summary["readback_chars"])
        self.assertIsNone(summary["sum_input_p50"])


class Record(unittest.TestCase):
    def test_record_writes_context_json_even_on_error(self):
        directory = Path(tempfile.mkdtemp(prefix="context-", dir=BASE))
        try:
            result = rc.record(directory, {"provider": "claude"})
            self.assertEqual(json.loads((directory / "context.json").read_text()), result)
            self.assertTrue(result["error"])
        finally:
            import shutil
            shutil.rmtree(directory)


class Backfill(unittest.TestCase):
    def test_backfill_measures_finished_passes_once(self):
        import shutil
        import subprocess
        data = Path(tempfile.mkdtemp(prefix="context-", dir=BASE))
        self.addCleanup(shutil.rmtree, data)
        def attempt(name, status, log=None, job=None):
            path = data / "runs" / "r1" / "attempts" / name
            path.mkdir(parents=True)
            if log is not None:
                write(path / "payload.log", log)
            (path / "job.json").write_text(json.dumps(job or {"provider": "claude"}))
            (path / "status.json").write_text(json.dumps(status))
            return path
        boot = open("/proc/sys/kernel/random/boot_id").read().strip()
        gone = {"boot": boot, "pid": 4194303, "start": 1}                     # a guardian that has exited
        done = attempt("a", dict(gone, state="terminal"), CLAUDE)
        missing = attempt("b", dict(gone, state="terminal"))                   # no log yet: retried
        damaged = attempt("c", dict(gone, state="terminal"), CLAUDE[:3] + ['{"trunc'])  # never retried
        running = attempt("d", dict(gone, state="running"), CLAUDE)            # dead, but only just
        killed = attempt("g", dict(gone, state="running"), CLAUDE)             # dead long since: measured
        old = time.time() - 3600
        os.utime(killed / "status.json", (old, old))
        # a guardian still alive may hold locks: left until it exits
        live = attempt("e", {"state": "terminal", "boot": boot,
                             "pid": os.getpid(),
                             "start": int(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19])}, CLAUDE)
        attempt("f", dict(gone, state="terminal"), CLAUDE, job=["not", "a", "job"])
        def backfill():
            r = subprocess.run([sys.executable, str(Path(rc.__file__).parent / "review-context.py"),
                                "--data", str(data)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()
        self.assertEqual(backfill(), "context: 2 measured, 2 unknown")
        self.assertEqual(json.loads((done / "context.json").read_text())["requests"], 2)
        self.assertTrue((killed / "context.json").exists())
        self.assertFalse((running / "context.json").exists())
        self.assertFalse((live / "context.json").exists())
        self.assertTrue((damaged / "context.json").exists())
        before = (missing / "context.json").stat().st_mtime_ns
        self.assertEqual(backfill(), "context: 0 measured, 1 unknown")      # only the missing log again
        # an unchanged retry does not rewrite it, so GC sees the attempt's true age
        self.assertEqual((missing / "context.json").stat().st_mtime_ns, before)
        import fcntl
        with open(data / "metrics" / "context.lock", "a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            self.assertEqual(backfill(), "context: another collector is running")


if __name__ == "__main__":
    unittest.main()

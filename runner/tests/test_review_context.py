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

    def test_schema_and_orientation_signals(self):
        def call(ident, command, output):
            return [{"type": "assistant", "message": {"id": "m" + ident, "usage": usage(1, 1, 1), "content": [
                        {"type": "tool_use", "id": ident, "name": "Bash", "input": {"command": command}}]}},
                    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident,
                                                              "content": output}]}}]
        rows = (call("a", "cat /x/runner/bin/review_schema.py | head -400", "def validate") +
                call("b", "python3 /x/review_schema.py check /j/review.json", "invalid: verdict") +
                # two checks in one call, after a failure: both are repairs
                call("c", "cd /j && python3 /x/review_schema.py check a.json; python3 /x/review_schema.py check b.json",
                     "valid\ninvalid: gaps") +
                call("d", "pwd; ls -la", "/j") +
                call("e", "pwd; python3 -c 1", "x") +
                # mentioning the check is not running it; env running a command is not looking around
                call("f", "echo review_schema.py check", "review_schema.py check") +
                call("g", "env python3 /x/review_schema.py check r.json", "valid") +
                # neither runs the check
                call("h", "env echo review_schema.py check r.json", "") +
                call("i", "python3 other.py review_schema.py check r.json", ""))
        write(self.dir / "payload.log", rows)
        (self.dir / "status.json").write_text(json.dumps({"result_status": "invalid"}))
        m = rc.measure(self.dir, {"provider": "claude"})
        self.assertEqual(m["signals"], dict(schema_source_reads=1, schema_checks=4, schema_check_failures=2,
                                            schema_repairs=3, orienting_calls=1, orienting_commands=3,
                                            unreadable_commands=0, job_json_reads=0, job_json_read_chars=0,
                                            inputs_reads=0, inputs_read_chars=0))
        self.assertEqual(m["result_status"], "invalid")

    def test_only_a_real_check_invocation_counts(self):
        yes = ["python3 /x/review_schema.py check r.json", "/x/review_schema.py check r.json",
               "env -u PYTHONPATH python3 /x/review_schema.py check r.json", "A=1 python3 -u /x/review_schema.py check r",
               "python3 -W ignore /x/review_schema.py check r", "env python3 -X dev /x/review_schema.py check r"]
        no = ["python3 -c 'review_schema.py' check r.json", "python3 -m review_schema.py check r.json",
              "python3 -Bc x /x/review_schema.py check r", "env echo review_schema.py check r.json",
              "python3 other.py review_schema.py check r.json", "env -S 'python3 /x/review_schema.py check r'"]
        for command in yes:
            self.assertTrue(rc.checks_in(rc.commands(command)[0]), command)
        for command in no:
            self.assertFalse(rc.checks_in(rc.commands(command)[0]), command)

    def test_a_check_skipped_after_a_failure_is_not_counted(self):
        rows = [{"type": "assistant", "message": {"id": "m1", "usage": usage(1, 1, 1), "content": [
                    {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command":
                     "python3 /x/review_schema.py check a.json && python3 /x/review_schema.py check b.json"}}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a",
                                                          "content": "invalid: verdict"}]}}]
        write(self.dir / "payload.log", rows)
        signals = rc.measure(self.dir, {"provider": "claude"})["signals"]
        self.assertEqual((signals["schema_checks"], signals["schema_check_failures"], signals["schema_repairs"]),
                         (1, 1, 0))

    def test_reads_of_job_json_and_of_the_rendered_inputs(self):
        def call(ident, name, given, output="x"):
            return [{"type": "assistant", "message": {"id": "m" + ident, "usage": usage(1, 1, 1), "content": [
                        {"type": "tool_use", "id": ident, "name": name, "input": given}]}},
                    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident,
                                                              "content": output}]}}]
        rows = (call("a", "Bash", {"command": "python3 -c \"import json;json.load(open('job.json'))\""}, "x" * 10) +
                call("b", "Read", {"file_path": "/j/inputs/thread.md"}, "y" * 20) +
                call("c", "Bash", {"command": "sed -n 1,50p inputs/diff/0001.patch inputs/facts.md"}, "z" * 30) +
                call("d", "Read", {"file_path": "/j/job.json"}, "w" * 40) +
                call("e", "Read", {"file_path": "inputs/facts.md"}, "v" * 5) +
                # mentioning, copying or searching for a name is not reading it
                call("f", "Bash", {"command": "echo job.json inputs/facts.md; cp inputs/result-skeleton.json r.json"}) +
                call("g", "Bash", {"command": "python3 -c \"print('job.json')\"; rg 'inputs/facts.md' README.md"}) +
                call("h", "Bash", {"command": "python3 -c \"import shutil;shutil.copy('job.json','x')\""}) +
                call("i", "Bash", {"command": "python3 -c \"import json;json.loads('\\\"job.json\\\"')\""}))
        write(self.dir / "payload.log", rows)
        (self.dir / "inputs").mkdir()
        (self.dir / "inputs" / "facts.md").write_text("12345")
        m = rc.measure(self.dir, {"provider": "claude"})
        signals = m["signals"]
        self.assertEqual((signals["job_json_reads"], signals["job_json_read_chars"]), (2, 50))
        self.assertEqual((signals["inputs_reads"], signals["inputs_read_chars"]), (4, 55))
        self.assertEqual(m["inputs_bytes"], 5)

    def test_reader_arguments_are_parsed_as_the_reader_does(self):
        cases = {("grep", "-efoo job.json"): ["job.json"], ("sed", "-ne'1,20p' inputs/facts.md"): ["inputs/facts.md"],
                 ("rg", "--regexp=foo inputs/facts.md"): ["inputs/facts.md"],
                 ("rg", "-g '*.md' 'inputs/facts.md' ."): ["."], ("grep", "-A 3 pat job.json"): ["job.json"],
                 ("sed", "-n 1,5p inputs/x.md"): ["inputs/x.md"], ("cat", "-n inputs/a.md inputs/b.md"):
                     ["inputs/a.md", "inputs/b.md"],
                 ("rg", "--encoding utf-8 job.json README.md"): ["README.md"],
                 ("grep", "-- -e job.json"): ["job.json"], ("grep", "--include '*.md' -r x inputs/"): ["inputs/"],
                 ("grep", "--color foo job.json"): ["job.json"], ("rg", "--color never foo job.json"): ["job.json"]}
        for (program, args), files in cases.items():
            words = rc.commands(program + " " + args)[0]
            self.assertEqual(rc.reader_files(program, words[1:]), files, (program, args))

    def test_a_failure_then_a_check_in_one_call_is_a_repair(self):
        rows = [{"type": "assistant", "message": {"id": "m1", "usage": usage(1, 1, 1), "content": [
                    {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command":
                     "python3 /x/review_schema.py check r.json; python3 /x/review_schema.py check r.json"}}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "a",
                                                          "content": "invalid: verdict\nvalid"}]}}]
        write(self.dir / "payload.log", rows)
        signals = rc.measure(self.dir, {"provider": "claude"})["signals"]
        self.assertEqual((signals["schema_checks"], signals["schema_check_failures"], signals["schema_repairs"]),
                         (2, 1, 1))

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
        self.assertEqual(m["signals"]["orienting_calls"], 1)              # pwd; printenv; env
        self.assertEqual(m["signals"]["orienting_commands"], 3)
        self.assertEqual(m["tools"]["Bash:sed"]["chars"], 300)

    def test_older_records_count_a_change_in_the_total_not_a_repeat(self):
        # a fall in the total is a reset (a resume), and still a request
        write(self.rollout, codex_rows([count(100, 90, 0), count(100, 90, 0), count(250, 140, 80),
                                        count(250, 140, 80), count(30, 20, 0)]))
        m = rc.measure(self.dir, self.job)
        self.assertEqual(m["requests"], 3)
        self.assertEqual(m["sum_input"], 90 + 140 + 20)

    def test_a_check_failure_inside_codex_json_output_is_seen(self):
        calls = [{"type": "response_item", "payload": {"type": "function_call", "call_id": "k", "name": "shell",
                   "arguments": json.dumps({"cmd": "python3 /x/review_schema.py check r.json"})}},
                 {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "k",
                   "output": json.dumps({"exit_code": 1, "output": "invalid: verdict\n"})}}]
        write(self.rollout, codex_rows([tur("r1", 10, 0)] + calls))
        signals = rc.measure(self.dir, self.job)["signals"]
        self.assertEqual((signals["schema_checks"], signals["schema_check_failures"]), (1, 1))

    def test_code_mode_output_blocks_are_each_decoded(self):
        check = "python3 /x/review_schema.py check r.json"
        cases = [  # (output blocks, checks, failures, repairs)
            (["Script completed\nOutput:\n", json.dumps({"exit_code": 1, "output": "invalid: verdict\n"})], 1, 1, 0),
            (["Script completed\n" + json.dumps({"output": "invalid: verdict\n"}) + "\n"
              + json.dumps({"output": "valid\n"})], 2, 1, 1),
            (["Script completed\nOutput:\n", "invalid: verdict\nvalid\n"], 2, 1, 1)]
        for blocks, checks, failures, repairs in cases:
            calls = [{"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "k", "name": "exec",
                       "input": 'await tools.exec_command({cmd:"%s"}); await tools.exec_command({cmd:"%s"})' % (check, check)}},
                     {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "k",
                       "output": [{"type": "input_text", "text": b} for b in blocks]}}]
            write(self.rollout, codex_rows([tur("r1", 10, 0)] + calls))
            signals = rc.measure(self.dir, self.job)["signals"]
            self.assertEqual((signals["schema_checks"], signals["schema_check_failures"], signals["schema_repairs"]),
                             (checks, failures, repairs), blocks)

    def test_every_javascript_string_form_of_a_command_is_read(self):
        for literal in ("'python3 /x/review_schema.py check r.json'", '"python3 /x/review_schema.py check r.json"',
                        "`python3 /x/review_schema.py check r.json`"):
            calls = [{"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "k", "name": "exec",
                       "input": "await tools.exec_command({cmd: %s})" % literal}},
                     {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "k",
                       "output": [{"type": "input_text", "text": "invalid: verdict\n"}]}}]
            write(self.rollout, codex_rows([tur("r1", 10, 0)] + calls))
            m = rc.measure(self.dir, self.job)
            self.assertEqual((m["signals"]["schema_checks"], m["signals"]["schema_check_failures"]), (1, 1), literal)
            self.assertEqual(set(m["tools"]), {"Bash:python3"}, literal)
        # one that cannot be read is counted as such, not as nothing
        calls = [{"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "k", "name": "exec",
                   "input": "await tools.exec_command({cmd: `cat ${file}`})"}}]
        write(self.rollout, codex_rows([tur("r1", 10, 0)] + calls))
        self.assertEqual(rc.measure(self.dir, self.job)["signals"]["unreadable_commands"], 1)
        # polling a running session runs no command at all
        calls = [{"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "k", "name": "exec",
                   "input": 'text(await tools.write_stdin({session_id:9,chars:""}))'}}]
        write(self.rollout, codex_rows([tur("r1", 10, 0)] + calls))
        self.assertEqual(rc.measure(self.dir, self.job)["signals"]["unreadable_commands"], 0)

    def test_disagreeing_request_sources_make_the_measurement_unknown(self):
        write(self.rollout, codex_rows([tur("r1", 10, 0), count(15, 10, 0), count(40, 20, 0)]))
        m = rc.measure(self.dir, self.job)
        self.assertIn("disagree", m["error"])
        self.assertFalse(rc.known(m))

    def test_a_compaction_request_missing_from_the_totals_is_not_disagreement(self):
        # as the CLI writes it: the compaction request has a usage record, then
        # the total stays put with zero last usage
        rows = [tur("r1", 100, 0), count(105, 100, 0), tur("r2", 246, 0), {"type": "compacted"},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"total_tokens": 105},
                    "last_token_usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}}}},
                tur("r3", 20, 0), count(130, 20, 0)]
        write(self.rollout, codex_rows(rows))
        m = rc.measure(self.dir, self.job)
        self.assertIsNone(m["error"])
        self.assertEqual((m["requests"], m["sum_input"], m["compactions"]), (3, 366, 1))
        self.assertTrue(m["warnings"])
        # without the compaction, the same gap is disagreement
        write(self.rollout, codex_rows([r for r in rows if r != {"type": "compacted"}]))
        self.assertIn("disagree", rc.measure(self.dir, self.job)["error"])
        # a compaction elsewhere does not excuse an ordinary request the totals describe wrongly
        wrong = [tur("a", 100, 0), count(105, 100, 0), tur("c", 20, 0), {"type": "compacted"},
                 tur("b", 999, 0), count(130, 20, 0)]
        write(self.rollout, codex_rows(wrong))
        self.assertIn("disagree", rc.measure(self.dir, self.job)["error"])

    def test_a_repeated_record_keeps_its_place_before_a_compaction(self):
        # b is followed by a repeat of a, not by the compaction: b is not excused
        rows = [tur("a", 100, 0), count(105, 100, 0), tur("b", 50, 0), tur("a", 100, 0), {"type": "compacted"}]
        write(self.rollout, codex_rows(rows))
        self.assertIn("disagree", rc.measure(self.dir, self.job)["error"])

    def test_another_threads_compaction_excuses_nothing(self):
        write(self.dir / "payload.log", [{"type": "thread.started", "thread_id": "T1"},
                                         {"type": "thread.started", "thread_id": "T2"}])
        write(self.rollout, codex_rows([tur("a", 100, 0), count(105, 100, 0), tur("b", 50, 0)]))
        write(self.home / "sessions/2026/10/05/rollout-2026-10-05T08-00-00-T2.jsonl",
              codex_rows([{"type": "compacted"}, tur("d", 30, 0), count(40, 30, 0)]))
        self.assertIn("disagree", rc.measure(self.dir, self.job)["error"])

    def test_a_resumed_rollout_carries_the_running_total(self):
        zero = {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 105},
            "last_token_usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}}}}
        write(self.rollout, codex_rows([tur("a", 100, 0), count(105, 100, 0)]))
        write(self.home / "sessions/2026/10/06/rollout-2026-10-06T01-00-00-T1.jsonl",
              codex_rows([tur("c", 246, 0), {"type": "compacted"}, zero, tur("b", 20, 0), count(130, 20, 0)]))
        m = rc.measure(self.dir, self.job)
        self.assertIsNone(m["error"])
        self.assertEqual(m["requests"], 3)

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
        self.assertEqual(m["signals"]["orienting_calls"], 0)
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

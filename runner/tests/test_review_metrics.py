#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
import review_metrics as M  # noqa: E402

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")


class Metrics(unittest.TestCase):
    def test_endpoints_group_by_kind(self):
        self.assertEqual(M.github_class("repos/ArduPilot/ardupilot/pulls/32995"), "GET pulls/N")
        self.assertEqual(M.github_class("repos/a/b/commits/" + "c" * 40 + "/check-runs?per_page=100&page=2"),
                         "GET commits/SHA/check-runs")
        self.assertEqual(M.github_class("repos/a/b/issues/7/comments", "POST"), "POST issues/N/comments")
        self.assertEqual(M.github_class("graphql", "POST", "query { x }"), "graphql query")
        self.assertEqual(M.github_class("graphql", "POST", " mutation { y }"), "graphql mutation")
        self.assertEqual(M.github_class("search/issues?q=x"), "search")

    def test_counts_reach_the_store_once_per_flush_and_per_process(self):
        data = tempfile.mkdtemp()
        code = ("import sys; sys.path.insert(0, %r); import review_metrics as M\n"
                "M.context(process='drain', run='r1', data=%r)\n"
                "M.count('github', 'GET pulls/N', 0.5); M.count('github', 'GET pulls/N', 0.25)\n"
                "M.flush(); M.flush()\n"
                "M.count('site', 'rsync page', 3)\n") % (BIN, data)
        for _ in range(2):
            subprocess.run([sys.executable, "-c", code], check=True)
        [log] = os.listdir(os.path.join(data, "metrics"))
        lines = [json.loads(x) for x in open(os.path.join(data, "metrics", log))]
        # each process: one explicit flush, one at exit; empty flushes write nothing
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0]["counts"], {"github/GET pulls/N": [2, 0.75]})
        self.assertEqual(lines[1]["counts"], {"site/rsync page": [1, 3.0]})
        self.assertEqual({x["process"] for x in lines}, {"drain"})
        self.assertEqual(len({x["pid"] for x in lines}), 2)

    def test_scoped_work_is_counted_apart(self):
        M.count("github", "x")
        with M.scope("drain"):
            M.count("github", "x")
        self.assertEqual(M._counts.pop("github/x")[0], 1)
        self.assertEqual(M._counts.pop("drain:github/x")[0], 1)

    def test_a_failed_write_keeps_the_counts(self):
        M._counts.clear()
        M.context(data="/proc/no-such-dir", process="t")
        M.count("github", "y", 1.0)
        M.flush()
        self.assertEqual(M._counts["github/y"], [1, 1.0])
        M._counts.clear()
        M._context.clear()

    def test_a_short_write_keeps_the_counts_and_the_log_parseable(self):
        data = tempfile.mkdtemp()
        M._counts.clear()
        M.context(data=data, process="t")
        M.count("github", "z", 2.0)
        real = os.write
        calls = []
        def short(fd, b):
            calls.append(len(b))
            return real(fd, b[:5] if len(calls) == 1 else b)
        M.os.write = short
        try:
            M.flush()
        finally:
            M.os.write = real
        self.assertEqual(M._counts["github/z"], [1, 2.0])
        M.flush()
        [log] = os.listdir(os.path.join(data, "metrics"))
        good = []
        for line in open(os.path.join(data, "metrics", log)):
            try:
                good.append(json.loads(line))
            except ValueError:
                pass
        self.assertEqual([x["counts"] for x in good], [{"github/z": [1, 2.0]}])
        M._counts.clear()
        M._context.clear()

    def test_no_data_directory_writes_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "REVIEW_DATA"}
        cwd = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import review_metrics as M; "
                        "M.count('github', 'x')" % BIN], check=True, env=env, cwd=cwd)
        self.assertEqual(os.listdir(cwd), [])


if __name__ == "__main__":
    unittest.main()

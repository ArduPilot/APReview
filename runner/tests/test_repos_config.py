#!/usr/bin/env python3
"""Checks on repos.json - the one file you edit to add a repo.

These are the mistakes an edit here can make: a duplicate key silently merges
two repos' reviews in the manifest, a missing key makes a PR unaddressable, and
a repo that nothing sweeps is a repo nobody reviews.
"""
import importlib.util
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
spec = importlib.util.spec_from_file_location(
    "repos", os.path.join(ROOT, "runner", "bin", "repos.py"))
repos_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repos_mod)
CFG = json.load(open(os.path.join(ROOT, "repos.json")))
REPOS = CFG["repos"]


class Config(unittest.TestCase):
    def test_every_repo_has_the_fields_a_sweep_needs(self):
        for r in REPOS:
            for field in ("repo", "key", "discovery", "post_comments", "house_rules"):
                self.assertIn(field, r, "%s is missing %s" % (r.get("repo"), field))
            self.assertRegex(r["repo"], r"^[\w.-]+/[\w.-]+$")

    def test_manifest_keys_are_unique(self):
        keys = [r["key"] for r in REPOS]
        dupes = {k for k in keys if keys.count(k) > 1}
        self.assertFalse(dupes, "duplicate manifest keys merge two repos: %s" % dupes)

    def test_only_the_main_repo_has_the_empty_key(self):
        empty = [r["repo"] for r in REPOS if r["key"] == ""]
        self.assertEqual(empty, ["ArduPilot/ardupilot"])

    def test_the_two_mavlinks_do_not_collide(self):
        # the fork arrives via the submodule sweep keyed by basename
        keys = {r["key"] for r in REPOS}
        self.assertIn("upstream-mavlink", keys)
        self.assertNotIn("mavlink", keys)

    def test_house_rules_are_ones_the_command_knows(self):
        for r in REPOS:
            self.assertIn(r["house_rules"], ("ardupilot", "fork", "upstream", "none"))

    def test_discovery_says_how_each_repo_is_found(self):
        for r in REPOS:
            self.assertIn(r["discovery"], ("main", "explicit", "rsync-mode"))

    def test_a_repo_where_comments_are_held_says_why(self):
        for r in REPOS:
            if not r["post_comments"]:
                self.assertTrue(r.get("notes"),
                                "%s holds comments but says nothing about why" % r["repo"])

    def test_notes_exist_wherever_the_rules_are_not_ardupilots(self):
        for r in REPOS:
            if r["house_rules"] != "ardupilot":
                self.assertTrue(r.get("notes"),
                                "%s is judged by other conventions but has no notes" % r["repo"])


class CloneDirs(unittest.TestCase):
    """Where each base clone lands. Getting this wrong re-clones gigabytes."""

    def dirs(self):
        return {r["repo"]: (r.get("clone_dir") or r["repo"].split("/")[-1])
                for r in REPOS if r["discovery"] != "main"}

    def test_directories_are_unique(self):
        d = list(self.dirs().values())
        dupes = {x for x in d if d.count(x) > 1}
        self.assertFalse(dupes, "two repos would share a clone directory: %s" % dupes)

    def test_the_wiki_keeps_its_historical_directory(self):
        # its manifest key is "wiki"; the clone has always been ardupilot_wiki,
        # and naming it from the key would orphan 2.1G and re-download it
        self.assertEqual(self.dirs()["ArduPilot/ardupilot_wiki"], "ardupilot_wiki")

    def test_upstream_mavlink_does_not_collide_with_the_fork(self):
        self.assertEqual(self.dirs()["mavlink/mavlink"], "upstream-mavlink")


class Cli(unittest.TestCase):
    """Run repos.py as the scripts do. The previous version of these tests
    rebuilt the filters in Python and compared the result against itself, so
    replacing the --sweep body with `pass` left the whole suite green."""

    def run_it(self, *args):
        env = dict(os.environ, REVIEW_REPO_CONFIG=os.path.join(ROOT, "repos.json"))
        out = subprocess.run([sys.executable,
                              os.path.join(ROOT, "runner", "bin", "repos.py"), *args],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [l for l in out.stdout.splitlines() if l.strip()]

    def test_sweep_prints_every_explicitly_swept_repo(self):
        expected = {r["repo"] for r in REPOS if r["discovery"] in ("main", "explicit")}
        self.assertEqual(set(self.run_it("--sweep")), expected)
        self.assertTrue(expected, "a sweep of nothing publishes a confidently empty report")

    def test_sweep_excludes_the_rsync_target(self):
        # it is reviewed by its own mode, on its own account
        self.assertNotIn("RsyncProject/rsync", self.run_it("--sweep"))

    def test_clone_covers_everything_but_the_main_repo(self):
        clones = self.run_it("--clone")
        self.assertNotIn("ArduPilot/ardupilot", clones)
        self.assertIn("RsyncProject/rsync", clones)
        self.assertEqual(len(clones), len(REPOS) - 1)

    def test_clone_dirs_pairs_every_repo_with_a_directory(self):
        pairs = [l.split("\t") for l in self.run_it("--clone-dirs")]
        self.assertTrue(all(len(p) == 2 and p[1] for p in pairs))
        d = dict(pairs)
        self.assertEqual(d["ArduPilot/ardupilot_wiki"], "ardupilot_wiki")
        self.assertEqual(d["mavlink/mavlink"], "upstream-mavlink")

    def test_notes_prints_the_repos_own_guidance(self):
        out = "\n".join(self.run_it("--notes", "WebTools"))
        self.assertIn("no GitHub Actions workflows", out)

    def test_an_unknown_repo_is_an_error_not_silence(self):
        env = dict(os.environ, REVIEW_REPO_CONFIG=os.path.join(ROOT, "repos.json"))
        out = subprocess.run([sys.executable,
                              os.path.join(ROOT, "runner", "bin", "repos.py"),
                              "--notes", "NoSuchRepo"],
                             capture_output=True, text=True, env=env)
        self.assertNotEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

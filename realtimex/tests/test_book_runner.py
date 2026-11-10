"""Unit tests for realtimex/scripts/book_runner.py.

Run: python3 -m unittest discover -s realtimex/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import book_runner as br  # noqa: E402

L = br.DEFAULT_LABEL


def job(labels, name="Build mac-arm64 (fallback runner)", run_id=7):
    return {"id": 1, "name": name, "run_id": run_id, "labels": labels, "status": "queued"}


def runner(status, busy=False, labels=(L,), name="rtx-mini-x", rid=5):
    return {"id": rid, "name": name, "status": status, "busy": busy, "labels": [{"name": n} for n in labels]}


class FakeGitHub:
    def __init__(self, runners):
        self.runners = runners
        self.deleted = []
        self.posted = []

    def delete(self, path):
        self.deleted.append(path)

    def post(self, path, body):
        self.posted.append((path, body))
        return {"encoded_jit_config": "SECRET-CONFIG"}


class DecideTests(unittest.TestCase):
    def test_no_job_no_runner(self):
        plan = br.decide([], [], L)
        self.assertEqual((plan["needed"], plan["reason"]), (False, "no-queued-job"))

    def test_jobs_for_other_labels_are_ignored(self):
        plan = br.decide([job(["macos-15"]), job(["ubuntu-22.04"])], [], L)
        self.assertFalse(plan["needed"])

    def test_queued_fallback_job_books_a_runner(self):
        plan = br.decide([job([L])], [runner("offline")], L)
        self.assertEqual((plan["needed"], plan["reason"]), (True, "queued-job-without-runner"))
        self.assertEqual(plan["queuedJobs"][0]["runId"], 7)

    def test_idle_runner_online_takes_it(self):
        plan = br.decide([job([L])], [runner("online")], L)
        self.assertEqual((plan["needed"], plan["reason"]), (False, "runner-online"))

    def test_busy_runner_means_wait_one_build_at_a_time(self):
        plan = br.decide([job([L])], [runner("online", busy=True)], L)
        self.assertFalse(plan["needed"])

    def test_runner_with_other_labels_does_not_count(self):
        plan = br.decide([job([L])], [runner("online", labels=("self-hosted", "gitlab"))], L)
        self.assertTrue(plan["needed"])


class CleanupAndMintTests(unittest.TestCase):
    def test_cleanup_removes_only_offline_prefixed_runners(self):
        runners = [
            runner("offline", name="rtx-mini-1", rid=1),
            runner("online", name="rtx-mini-2", rid=2),
            runner("offline", name="someone-else", rid=3),
        ]
        gh = FakeGitHub(runners)
        removed = br.cleanup(gh, "o/r", runners, "rtx-mini-")
        self.assertEqual(removed, ["rtx-mini-1"])
        self.assertEqual(gh.deleted, ["repos/o/r/actions/runners/1"])

    def test_mint_registers_only_the_fallback_label(self):
        gh = FakeGitHub([])
        cfg = br.mint(gh, "o/r", L, "rtx-mini-", now=datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc))
        self.assertEqual(cfg, "SECRET-CONFIG")
        path, body = gh.posted[0]
        self.assertEqual(path, "repos/o/r/actions/runners/generate-jitconfig")
        self.assertEqual(body["labels"], [L])
        self.assertEqual(body["name"], "rtx-mini-20261109T233000Z")
        self.assertEqual(body["runner_group_id"], 1)


if __name__ == "__main__":
    unittest.main()

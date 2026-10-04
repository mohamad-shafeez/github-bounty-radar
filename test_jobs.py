#!/usr/bin/env python3
import tempfile
import unittest
from pathlib import Path

from jobs import (
    apply_action,
    create_discovered,
    empty_store,
    load_store,
    save_store,
    transition,
)


class JobTests(unittest.TestCase):
    def test_deterministic_id_and_accept(self):
        data = empty_store()
        a, created = create_discovered(data, repo="Expensify/App", number=123,
                                       title="Test", url="https://github.com/Expensify/App/issues/123")
        b, created2 = create_discovered(data, repo="Expensify/App", number=123,
                                        title="Changed", url="https://github.com/Expensify/App/issues/123")
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(a["job_id"], b["job_id"])
        apply_action(data, a["job_id"], "ACCEPT", actor="test")
        self.assertEqual(data["jobs"][a["job_id"]]["state"], "ACCEPTED")

    def test_invalid_skip_transition(self):
        data = empty_store()
        job, _ = create_discovered(data, repo="owner/repo", number=1, title="x", url="u")
        with self.assertRaises(ValueError):
            apply_action(data, job["job_id"], "APPROVE_IMPLEMENTATION")

    def test_idempotent_accept_repeated(self):
        data = empty_store()
        job, _ = create_discovered(data, repo="owner/repo", number=10, title="x", url="u")
        jid = job["job_id"]
        # First ACCEPT
        apply_action(data, jid, "ACCEPT")
        self.assertEqual(data["jobs"][jid]["state"], "ACCEPTED")

        # Advance job down pipeline
        transition(data, jid, "INGESTING")
        transition(data, jid, "INVESTIGATING")
        self.assertEqual(data["jobs"][jid]["state"], "INVESTIGATING")

        # Second ACCEPT must be idempotent and must NOT revert to ACCEPTED or fail
        apply_action(data, jid, "ACCEPT")
        self.assertEqual(data["jobs"][jid]["state"], "INVESTIGATING")

    def test_idempotent_decline(self):
        data = empty_store()
        job, _ = create_discovered(data, repo="owner/repo", number=11, title="x", url="u")
        jid = job["job_id"]
        apply_action(data, jid, "DECLINE")
        self.assertEqual(data["jobs"][jid]["state"], "DECLINED")
        # Repeated DECLINE
        apply_action(data, jid, "DECLINE")
        self.assertEqual(data["jobs"][jid]["state"], "DECLINED")

    def test_full_state_progression(self):
        data = empty_store()
        job, _ = create_discovered(data, repo="owner/repo", number=99, title="Full flow", url="u")
        jid = job["job_id"]
        self.assertEqual(job["state"], "PENDING_APPROVAL")

        transition(data, jid, "ACCEPTED")
        self.assertEqual(data["jobs"][jid]["state"], "ACCEPTED")

        transition(data, jid, "INGESTING")
        self.assertEqual(data["jobs"][jid]["state"], "INGESTING")

        transition(data, jid, "INVESTIGATING")
        self.assertEqual(data["jobs"][jid]["state"], "INVESTIGATING")

        transition(data, jid, "CROSS_REVIEW")
        self.assertEqual(data["jobs"][jid]["state"], "CROSS_REVIEW")

        transition(data, jid, "DIAGNOSIS_READY")
        self.assertEqual(data["jobs"][jid]["state"], "DIAGNOSIS_READY")

        transition(data, jid, "PROPOSAL_READY")
        self.assertEqual(data["jobs"][jid]["state"], "PROPOSAL_READY")

        transition(data, jid, "HUMAN_REVIEW")
        self.assertEqual(data["jobs"][jid]["state"], "HUMAN_REVIEW")

        transition(data, jid, "APPROVED")
        self.assertEqual(data["jobs"][jid]["state"], "APPROVED")

        transition(data, jid, "IMPLEMENTING")
        self.assertEqual(data["jobs"][jid]["state"], "IMPLEMENTING")

        transition(data, jid, "TESTING")
        self.assertEqual(data["jobs"][jid]["state"], "TESTING")

        transition(data, jid, "FINAL_REVIEW")
        self.assertEqual(data["jobs"][jid]["state"], "FINAL_REVIEW")

        transition(data, jid, "PR_READY")
        self.assertEqual(data["jobs"][jid]["state"], "PR_READY")

        transition(data, jid, "PR_OPENED")
        self.assertEqual(data["jobs"][jid]["state"], "PR_OPENED")

    def test_failed_job_recovery(self):
        data = empty_store()
        job, _ = create_discovered(data, repo="owner/repo", number=12, title="x", url="u")
        jid = job["job_id"]
        transition(data, jid, "ACCEPTED")
        transition(data, jid, "FAILED")
        self.assertEqual(data["jobs"][jid]["state"], "FAILED")

        # Recover from failure back to ACCEPTED or PENDING_APPROVAL
        transition(data, jid, "ACCEPTED")
        self.assertEqual(data["jobs"][jid]["state"], "ACCEPTED")

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "jobs.json"
            data = empty_store()
            create_discovered(data, repo="owner/repo", number=2, title="x", url="u")
            save_store(data, path)
            loaded = load_store(path)
            self.assertEqual(len(loaded["jobs"]), 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for final review safety gate before PR creation."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from final_review import main as final_review_main
from jobs import empty_store, create_discovered, save_store, transition


class FinalReviewTests(unittest.TestCase):
    def test_final_review_passes_with_passing_tests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = empty_store()
            job, _ = create_discovered(data, repo="owner/repo", number=77, title="T", url="u")
            jid = job["job_id"]
            transition(data, jid, "ACCEPTED")
            transition(data, jid, "INGESTING")
            transition(data, jid, "INVESTIGATING")
            transition(data, jid, "DIAGNOSIS_READY")
            transition(data, jid, "HUMAN_REVIEW")
            transition(data, jid, "APPROVED")
            transition(data, jid, "IMPLEMENTING")
            transition(data, jid, "TESTING")

            art_dir = root / "job-artifacts" / jid
            art_dir.mkdir(parents=True, exist_ok=True)
            patch_file = art_dir / "implementation.patch"
            patch_file.write_text("--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")

            test_rep_file = art_dir / "test-report.json"
            test_rep_file.write_text(json.dumps({
                "generated_at": "2026-10-03T12:00:00Z",
                "changed_paths": ["src/app.py"],
                "tests": [{"command": ["pytest"], "ok": True, "output": "1 passed"}]
            }), encoding="utf-8")

            job["artifacts"]["implementation_patch"] = str(patch_file.relative_to(root).as_posix())
            job["artifacts"]["test_report"] = str(test_rep_file.relative_to(root).as_posix())
            save_store(data, root / "jobs.json")

            old_cwd = Path.cwd()
            os.chdir(root)
            os.environ["JOB_ID"] = jid
            try:
                code = final_review_main()
                self.assertEqual(code, 0)
                loaded = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
                self.assertEqual(loaded["jobs"][jid]["state"], "PR_READY")
            finally:
                os.chdir(old_cwd)

    def test_final_review_fails_when_test_failed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = empty_store()
            job, _ = create_discovered(data, repo="owner/repo", number=78, title="T", url="u")
            jid = job["job_id"]
            transition(data, jid, "ACCEPTED")
            transition(data, jid, "INGESTING")
            transition(data, jid, "INVESTIGATING")
            transition(data, jid, "DIAGNOSIS_READY")
            transition(data, jid, "HUMAN_REVIEW")
            transition(data, jid, "APPROVED")
            transition(data, jid, "IMPLEMENTING")
            transition(data, jid, "TESTING")

            art_dir = root / "job-artifacts" / jid
            art_dir.mkdir(parents=True, exist_ok=True)
            patch_file = art_dir / "implementation.patch"
            patch_file.write_text("--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")

            test_rep_file = art_dir / "test-report.json"
            test_rep_file.write_text(json.dumps({
                "generated_at": "2026-10-03T12:00:00Z",
                "changed_paths": ["src/app.py"],
                "tests": [{"command": ["pytest"], "ok": False, "output": "FAILED test_auth"}]
            }), encoding="utf-8")

            job["artifacts"]["implementation_patch"] = str(patch_file.relative_to(root).as_posix())
            job["artifacts"]["test_report"] = str(test_rep_file.relative_to(root).as_posix())
            save_store(data, root / "jobs.json")

            old_cwd = Path.cwd()
            os.chdir(root)
            os.environ["JOB_ID"] = jid
            try:
                code = final_review_main()
                self.assertEqual(code, 1)
                loaded = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
                self.assertEqual(loaded["jobs"][jid]["state"], "FAILED")
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()

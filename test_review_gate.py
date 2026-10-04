#!/usr/bin/env python3
"""Tests for evidence-based review gate, citation validation, and disagreement handling."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from review_gate import check_disagreements, evaluate_review, main as review_gate_main
from jobs import empty_store, create_discovered, save_store, transition


class ReviewGateTests(unittest.TestCase):
    def test_evaluate_valid_review(self):
        obj = {
            "summary": "Fix NPE in auth",
            "root_cause_hypothesis": "Missing null check",
            "proposed_fix": "Add if token is None check",
            "confirmed_facts": ["Token can be None on expiration"],
            "unknowns": [],
            "evidence": ["FILE:src/auth.py", "ISSUE", "COMMENT:1"],
            "confidence": 85,
        }
        info, errs = evaluate_review(obj, Path("mock.json"))
        self.assertEqual(errs, [])
        self.assertIsNotNone(info)
        self.assertIn("FILE:src/auth.py", info["citations"])

    def test_evaluate_missing_code_evidence_rejected(self):
        # AI claims a diagnosis but cites NO file or repository evidence
        obj = {
            "summary": "AI opinion without code citation",
            "root_cause_hypothesis": "Some hypothetical bug",
            "proposed_fix": "Rewrite everything",
            "confirmed_facts": ["Fact"],
            "unknowns": [],
            "evidence": ["ISSUE"],  # Missing FILE: or WORKFLOW:
            "confidence": 95,
        }
        info, errs = evaluate_review(obj, Path("mock.json"))
        self.assertIsNone(info)
        self.assertTrue(any("no code/repository citations found" in e for e in errs))

    def test_disagreement_detection_halts_auto_progression(self):
        rev1 = {
            "provider": "gemini",
            "evidence": ["FILE:src/database.py", "ISSUE"],
            "root_cause_hypothesis": "Database lock contention",
            "confidence": 90,
        }
        rev2 = {
            "provider": "grok",
            "evidence": ["FILE:frontend/ui.js", "ISSUE"],
            "root_cause_hypothesis": "React state re-render loop",
            "confidence": 40,
        }
        disagreements, is_material = check_disagreements([rev1, rev2])
        self.assertTrue(is_material)
        self.assertTrue(any("Disagreement on affected files" in d for d in disagreements))
        self.assertTrue(any("Significant confidence divergence" in d for d in disagreements))

    def test_review_gate_full_run_with_disagreement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = empty_store()
            job, _ = create_discovered(data, repo="owner/repo", number=50, title="T", url="u")
            jid = job["job_id"]
            transition(data, jid, "ACCEPTED")
            transition(data, jid, "INGESTING")
            transition(data, jid, "INVESTIGATING")

            rev_dir = root / "job-artifacts" / jid / "reviews"
            rev_dir.mkdir(parents=True, exist_ok=True)

            r1_path = rev_dir / "gemini.json"
            r1_path.write_text(json.dumps({
                "provider": "gemini",
                "summary": "Backend bug",
                "root_cause_hypothesis": "Deadlock",
                "proposed_fix": "Release lock",
                "confirmed_facts": ["Lock acquired"],
                "unknowns": [],
                "evidence": ["FILE:backend/lock.py", "ISSUE"],
                "confidence": 90
            }), encoding="utf-8")

            r2_path = rev_dir / "grok.json"
            r2_path.write_text(json.dumps({
                "provider": "grok",
                "summary": "Frontend bug",
                "root_cause_hypothesis": "CSS z-index issue",
                "proposed_fix": "Change z-index",
                "confirmed_facts": ["Button hidden"],
                "unknowns": [],
                "evidence": ["FILE:frontend/styles.css", "ISSUE"],
                "confidence": 40
            }), encoding="utf-8")

            job["artifacts"]["reviews"] = [str(r1_path.as_posix()), str(r2_path.as_posix())]
            save_store(data, root / "jobs.json")

            old_cwd = Path.cwd()
            os.chdir(root)
            os.environ["JOB_ID"] = jid
            try:
                code = review_gate_main()
                self.assertEqual(code, 0)
                gate_report = json.loads((root / "job-artifacts" / jid / "review-gate.json").read_text(encoding="utf-8"))
                self.assertEqual(gate_report["verdict"], "REQUIRES_FURTHER_INVESTIGATION")
                self.assertTrue(gate_report["material_disagreement"])

                # Job state should remain CROSS_REVIEW / require further investigation, NOT auto-advance to DIAGNOSIS_READY
                loaded_jobs = json.loads((root / "jobs.json").read_text(encoding="utf-8"))
                self.assertEqual(loaded_jobs["jobs"][jid]["state"], "CROSS_REVIEW")
                self.assertEqual(loaded_jobs["jobs"][jid]["review_status"], "REQUIRES_FURTHER_INVESTIGATION")
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()

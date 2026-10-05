#!/usr/bin/env python3
"""End-to-end integration test of the entire GitHub bounty automation pipeline.

Mocks all external network requests (GitHub API, Gemini/OpenRouter API, ntfy).
Verifies complete state progression, engineering dossier creation, and PR creation gate.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jobs import empty_store, create_discovered, save_store, load_store
from control import apply as control_apply
from pipeline import accept_and_ingest
from ai_review import review_job
from review_gate import main as review_gate_main
from proposal import build as build_proposal
from maintainer_watch import main as maintainer_watch_main
from final_review import main as final_review_main


class EndToEndPipelineTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.copy()
        os.environ["GEMINI_API_KEY"] = "mock_gemini_key"
        os.environ["OPENROUTER_API_KEY"] = "mock_openrouter_key"
        os.environ["AI_PROVIDERS"] = "gemini,openrouter"
        os.environ["AI_MAX_RETRIES"] = "1"
        os.environ["UPSTREAM_GITHUB_TOKEN"] = "mock_upstream_token"
        os.environ["NTFY_TOPIC"] = "mock_topic"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    @patch("requests.post")
    @patch("requests.Session.post")
    @patch("requests.Session.get")
    def test_e2e_investigation_stops_at_human_review(self, mock_session_get, mock_session_post, mock_requests_post):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = empty_store()

            # 1. Radar discovers issue
            job, created = create_discovered(
                data,
                repo="Expensify/App",
                number=9999,
                title="[$250] Crash on empty search report",
                url="https://github.com/Expensify/App/issues/9999",
                score=45,
                bounty={"amount": 250, "currency": "$", "status": "confirmed"},
            )
            jid = job["job_id"]
            save_store(data, root / "jobs.json")
            self.assertEqual(job["state"], "PENDING_APPROVAL")

            # 2. Human accepts bounty job via control
            control_apply("ACCEPT", jid, root)
            data = load_store(root / "jobs.json")
            self.assertEqual(data["jobs"][jid]["state"], "ACCEPTED")

            # 3. Ingestion mock responses
            def mock_gh_get_router(url, **kwargs):
                r = MagicMock()
                r.status_code = 200
                if "/issues/9999/comments" in url:
                    r.json.return_value = [{"id": 101, "user": {"login": "reporter"}, "body": "Steps: click search"}]
                elif "/issues/9999/timeline" in url:
                    r.json.return_value = []
                elif "/issues/9999" in url:
                    r.json.return_value = {"number": 9999, "title": "Crash", "body": "Crash report", "user": {"login": "reporter"}}
                elif "/contents/" in url:
                    # Return base64 encoded "def search(): pass"
                    import base64
                    encoded = base64.b64encode(b"def search(): pass").decode()
                    r.json.return_value = {"encoding": "base64", "content": encoded}
                elif "/git/trees/" in url:
                    r.json.return_value = {"tree": [{"path": "src/search.py", "type": "blob"}, {"path": "package.json", "type": "blob"}]}
                elif "/commits" in url:
                    r.json.return_value = [{"sha": "deadbeef", "commit": {"message": "Initial commit"}}]
                elif "/repos/Expensify/App" in url:
                    r.json.return_value = {"default_branch": "main", "full_name": "Expensify/App"}
                else:
                    r.json.return_value = {}
                return r

            mock_session_get.side_effect = mock_gh_get_router

            ingest_path = accept_and_ingest(jid, root, token="mock_gh_token")
            self.assertTrue(ingest_path.exists())
            data = load_store(root / "jobs.json")
            self.assertEqual(data["jobs"][jid]["state"], "INVESTIGATING")

            # 4. AI review mock responses (both Gemini and OpenRouter)
            def mock_req_post_router(url, **kwargs):
                r = MagicMock()
                r.status_code = 200
                if "generativelanguage.googleapis.com" in url:
                    r.json.return_value = {
                        "candidates": [{
                            "content": {
                                "parts": [{"text": json.dumps({
                                    "summary": "Gemini verified NPE in search.py",
                                    "root_cause_hypothesis": "Missing empty check",
                                    "confirmed_facts": ["search.py raises Exception"],
                                    "unknowns": [],
                                    "evidence": ["FILE:src/search.py", "ISSUE"],
                                    "proposed_fix": "Add empty list check in search()",
                                    "risks": [],
                                    "tests_to_run": ["test_empty_search"],
                                    "confidence": 90
                                })}]
                            }
                        }]
                    }
                else:
                    # OpenRouter
                    r.json.return_value = {
                        "choices": [{
                            "message": {
                                "content": json.dumps({
                                    "summary": "OpenRouter verified crash in search.py",
                                    "root_cause_hypothesis": "Missing empty check in search",
                                    "confirmed_facts": ["search.py raises Exception"],
                                    "unknowns": [],
                                    "evidence": ["FILE:src/search.py", "ISSUE"],
                                    "proposed_fix": "Add empty check in search()",
                                    "risks": [],
                                    "tests_to_run": ["test_empty_search"],
                                    "confidence": 88
                                })
                            }
                        }]
                    }
                return r

            mock_requests_post.side_effect = mock_req_post_router

            review_outputs = review_job(root, jid)
            self.assertEqual(len(review_outputs), 2)
            data = load_store(root / "jobs.json")
            self.assertEqual(data["jobs"][jid]["state"], "CROSS_REVIEW")

            # 5. Review gate
            old_cwd = Path.cwd()
            os.chdir(root)
            os.environ["JOB_ID"] = jid
            try:
                gate_code = review_gate_main()
                self.assertEqual(gate_code, 0)
                data = load_store(root / "jobs.json")
                self.assertEqual(data["jobs"][jid]["state"], "DIAGNOSIS_READY")

                # 6. Proposal & Dossier Generation
                prop_path = build_proposal(jid, root)
                self.assertTrue(prop_path.exists())
                data = load_store(root / "jobs.json")
                self.assertEqual(data["jobs"][jid]["state"], "HUMAN_REVIEW")

                # Check dossier was generated
                dossier_path = root / "job-artifacts" / jid / "issue-9999.md"
                self.assertTrue(dossier_path.exists())
                dossier_text = dossier_path.read_text(encoding="utf-8")
                self.assertIn("# Issue 9999 —", dossier_text)
                self.assertIn("## 1. Issue Summary", dossier_text)
                self.assertIn("## 19. Pull Request", dossier_text)

                # 7. Controlled-test safety boundary: STOP at HUMAN_REVIEW.
                # No proposal comment, maintainer message, implementation, or PR
                # is allowed during this E2E test.
                data = load_store(root / "jobs.json")
                self.assertEqual(data["jobs"][jid]["state"], "HUMAN_REVIEW")
                self.assertNotIn("posted_comment", data["jobs"][jid].get("artifacts", {}))
                self.assertNotIn("pull_request", data["jobs"][jid].get("artifacts", {}))
                self.assertNotIn("implementation", data["jobs"][jid].get("artifacts", {}))

            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()

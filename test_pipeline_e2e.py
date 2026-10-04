#!/usr/bin/env python3
"""End-to-end integration test of the entire GitHub bounty automation pipeline.

Mocks all external network requests (GitHub API, Gemini/Grok API, ntfy).
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
        os.environ["GROK_API_KEY"] = "mock_grok_key"
        os.environ["AI_PROVIDERS"] = "gemini,grok"
        os.environ["AI_MAX_RETRIES"] = "1"
        os.environ["UPSTREAM_GITHUB_TOKEN"] = "mock_upstream_token"
        os.environ["NTFY_TOPIC"] = "mock_topic"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    @patch("requests.post")
    @patch("requests.Session.post")
    @patch("requests.Session.get")
    def test_e2e_investigation_to_proposal(self, mock_session_get, mock_session_post, mock_requests_post):
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

            # 4. AI review mock responses (both Gemini and Grok)
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
                    # Grok
                    r.json.return_value = {
                        "choices": [{
                            "message": {
                                "content": json.dumps({
                                    "summary": "Grok verified crash in search.py",
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

                # 7. Post comment via control
                mock_comment_resp = MagicMock()
                mock_comment_resp.status_code = 201
                mock_comment_resp.json.return_value = {"id": 555, "html_url": "https://github.com/Expensify/App/issues/9999#issuecomment-555"}
                mock_session_post.return_value = mock_comment_resp
                mock_requests_post.side_effect = None
                mock_requests_post.return_value.status_code = 200

                control_apply("POST_COMMENT", jid, root)
                data = load_store(root / "jobs.json")
                self.assertEqual(data["jobs"][jid]["state"], "AWAITING_MAINTAINER")
                self.assertEqual(data["jobs"][jid]["artifacts"]["posted_comment"]["id"], 555)

                # 8. Maintainer watch observes comment from maintainer
                def mock_watch_comments(url, **kwargs):
                    r = MagicMock()
                    r.status_code = 200
                    r.json.return_value = [
                        {"id": 555, "user": {"login": "bounty-bot"}, "body": "Proposal"},
                        {"id": 556, "user": {"login": "mallenexpensify"}, "author_association": "MEMBER", "body": "Agreed, please implement!", "created_at": "2026-10-03T14:00:00Z", "html_url": "url"},
                    ]
                    return r

                mock_session_get.side_effect = mock_watch_comments
                mock_requests_post.return_value.status_code = 200

                maintainer_watch_main()
                data = load_store(root / "jobs.json")
                self.assertEqual(len(data["jobs"][jid]["maintainer_activity"]), 1)
                self.assertEqual(data["jobs"][jid]["maintainer_activity"][0]["author"], "mallenexpensify")

                # Verify dossier Section 16 was updated with maintainer response
                updated_dossier = dossier_path.read_text(encoding="utf-8")
                self.assertIn("Agreed, please implement!", updated_dossier)

                # 9. Human signals maintainer agreed, then approves implementation
                control_apply("MAINTAINER_AGREED", jid, root)
                data = load_store(root / "jobs.json")
                self.assertEqual(data["jobs"][jid]["state"], "MAINTAINER_AGREED")

                control_apply("APPROVE_IMPLEMENTATION", jid, root)
                data = load_store(root / "jobs.json")
                self.assertEqual(data["jobs"][jid]["state"], "APPROVED")

            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()

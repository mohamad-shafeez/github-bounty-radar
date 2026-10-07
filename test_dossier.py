#!/usr/bin/env python3
"""Tests for the 19-section Markdown Engineering Dossier and secret sanitization."""
import os
import tempfile
import unittest
from pathlib import Path

from dossier import (
    build_dossier_content,
    save_dossier,
    sanitize_secrets,
    update_dossier_maintainer_response,
    update_dossier_implementation_result,
    update_dossier_final_review,
    update_dossier_pull_request,
)


class DossierTests(unittest.TestCase):
    def test_all_19_sections_present(self):
        content = build_dossier_content(
            issue_number=101,
            title="Crash in auth flow",
            summary_what="Null pointer in token validation",
            summary_where="src/auth.py",
            summary_when="On expired session refresh",
            summary_who="Authenticated mobile users",
            original_issue="The app crashes with NPE when token expires.",
            expected_behavior="User should be redirected to login.",
            actual_behavior="App crashes with NullPointerException.",
            root_cause="Missing check for None before accessing token.expires_at",
            files_inspected=["src/auth.py", "tests/test_auth.py"],
            functions_modules=["validate_token() in src/auth.py"],
            code_evidence=["FILE:src/auth.py#L42"],
            tests_evidence=["tests/test_auth.py"],
            workflows_evidence=[".github/workflows/ci.yml"],
            manifests_evidence=["pyproject.toml"],
            git_history_evidence=["abc1234 - Add token refresh logic"],
            comments_context=["@maintainer: We can reproduce on Android."],
            related_prs=["#99 - Initial token refresh implementation"],
            bot_or_prior_work="No bot proposals found.",
            ai_reviewer_findings=[{
                "provider": "gemini",
                "model": "gemini-2.5-flash",
                "summary": "Confirmed null pointer bug in token refresh.",
                "evidence": ["FILE:src/auth.py", "ISSUE"],
                "confidence": 90,
            }],
            disagreements=[],
            disagreements_resolved=True,
            verified_diagnosis_cause="Token expiration timestamp check lacks None-guard.",
            verified_confidence="90%",
            verified_evidence=["FILE:src/auth.py", "ISSUE"],
            proposed_fix="Add `if token is None: return False` check before expiration comparison.",
            exact_files_to_change=["src/auth.py"],
            implementation_plan=["Add null check in validate_token", "Run tests/test_auth.py"],
            tests_required=["Unit test for None token handling"],
            maintainer_proposal="Proposed fix and evidence comment.",
        )

        expected_sections = [
            "## 1. Issue Summary",
            "## 2. Original Issue",
            "## 3. Reproduction Steps",
            "## 4. Root Cause",
            "## 5. Repository Evidence",
            "## 6. Issue Comments and Maintainer Context",
            "## 7. Related PRs / Existing Work",
            "## 8. AI Reviewer Findings",
            "## 9. Disagreements",
            "## 10. Verified Diagnosis",
            "## 11. Proposed Fix",
            "## 12. Exact Files To Change",
            "## 13. Implementation Plan",
            "## 14. Tests Required",
            "## 15. Maintainer Proposal",
            "## 16. Maintainer Response",
            "## 17. Implementation Result",
            "## 18. Final Review",
            "## 19. Pull Request",
        ]

        for sec in expected_sections:
            self.assertIn(sec, content, f"Missing section: {sec}")

    def test_secrets_sanitized_in_dossier(self):
        os.environ["SECRET_ENV_KEY"] = "super_secret_pat_99887766"
        dirty_text = (
            "Here is the token: ghp_111122223333444455556666777788889999 and "
            "Gemini key AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6 and "
            "super_secret_pat_99887766 and "
            "Authorization: Bearer mysecrettoken123456"
        )
        cleaned = sanitize_secrets(dirty_text)
        self.assertNotIn("ghp_111122223333444455556666777788889999", cleaned)
        self.assertNotIn("AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6", cleaned)
        self.assertNotIn("super_secret_pat_99887766", cleaned)
        self.assertNotIn("mysecrettoken123456", cleaned)
        self.assertIn("[REDACTED_CREDENTIAL]", cleaned)

    def test_dossier_lifecycle_updates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            job_id = "testjob123"
            issue_num = 456

            initial_content = build_dossier_content(issue_number=issue_num, title="Test Issue")
            save_dossier(root, job_id, issue_num, initial_content)

            # Update section 16: Maintainer response
            update_dossier_maintainer_response(root, job_id, issue_num, [{
                "author": "lead-maintainer",
                "association": "OWNER",
                "created_at": "2026-10-03T12:00:00Z",
                "body": "Looks great, please open a PR with tests!",
            }])

            # Update section 17: Implementation
            update_dossier_implementation_result(root, job_id, issue_num, ["src/auth.py"], [{"command": ["pytest"], "ok": True}])

            # Update section 18: Final review
            update_dossier_final_review(root, job_id, issue_num, {"ok": True, "generated_at": "2026-10-03T12:10:00Z", "changed_paths": ["src/auth.py"]})

            # Update section 19: PR
            update_dossier_pull_request(root, job_id, issue_num, {"number": 888, "url": "https://github.com/org/repo/pull/888"})

            final_text = (root / "job-artifacts" / job_id / f"issue-{issue_num}.md").read_text(encoding="utf-8")
            self.assertIn("lead-maintainer", final_text)
            self.assertIn("Implementation Status: SUCCESS", final_text)
            self.assertIn("**Review Status**: PASSED", final_text)
            self.assertIn("#888", final_text)


if __name__ == "__main__":
    unittest.main()

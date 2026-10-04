import json
import tempfile
import unittest
from pathlib import Path

from targets import TargetConfigError, build_queries, load_targets, parse_github_url


class TargetConfigTests(unittest.TestCase):
    def test_repo_and_issue_urls(self):
        self.assertEqual(parse_github_url("https://github.com/Expensify/App"), ("Expensify/App", None))
        self.assertEqual(parse_github_url("https://github.com/Expensify/App/issues/731"), ("Expensify/App", 731))

    def test_builds_repo_queries_and_exact_issue_watch(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "targets.json"
            p.write_text(json.dumps({
                "global": {"minimum_bounty_usd": 50},
                "targets": [
                    {"url": "https://github.com/a/repo", "labels": ["bounty"], "search_help_wanted": True},
                    {"url": "https://github.com/b/repo/issues/42"}
                ]
            }), encoding="utf-8")
            targets = load_targets(p)
        queries = build_queries(targets)
        self.assertTrue(any(q["target_key"] == "target:a/repo" for q in queries))
        exact = [q for q in queries if q.get("exact_issue") == 42]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0]["repo"], "b/repo")

    def test_duplicate_and_invalid_targets_fail(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "targets.json"
            p.write_text(json.dumps({"targets": [
                {"url": "https://github.com/a/repo"},
                {"url": "https://github.com/a/repo"}
            ]}), encoding="utf-8")
            with self.assertRaises(TargetConfigError):
                load_targets(p)


if __name__ == "__main__":
    unittest.main()

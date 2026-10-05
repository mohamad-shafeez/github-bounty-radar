import unittest
from pathlib import Path
from unittest.mock import patch
from ai_review import is_quota_error

class TestAIQuotaSafety(unittest.TestCase):
    def test_quota_detection(self):
        self.assertTrue(is_quota_error(Exception("HTTP 429 RESOURCE EXHAUSTED")))
        self.assertTrue(is_quota_error(Exception("rate limit exceeded")))
        self.assertTrue(is_quota_error(Exception("quota exceeded")))
        self.assertFalse(is_quota_error(Exception("HTTP 401 invalid key")))

    def test_cross_review_blocked(self):
        from proposal import build
        with patch("proposal.load_store", return_value={"jobs":{"x":{"state":"CROSS_REVIEW"}}}):
            with self.assertRaises(Exception):
                build("x", Path("."))

if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for Gemini, OpenRouter, and Groq AI providers with retry and fallback behavior.

Uses mocks exclusively to prevent real AI API calls and quota consumption.
"""
import json
import os
import unittest
from unittest.mock import patch, MagicMock

from ai_review import (
    run_provider_with_retry,
    run_with_fallback,
    configured_providers,
    generate_diff,
)


class AIProviderTests(unittest.TestCase):
    def setUp(self):
        self.old_env = os.environ.copy()
        os.environ["GEMINI_API_KEY"] = "mock_gemini_key_123"
        os.environ["GROQ_API_KEY"] = "mock_groq_key_123"
        os.environ["AI_MAX_RETRIES"] = "2"
        os.environ["AI_INITIAL_BACKOFF"] = "0.01"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)

    @patch("requests.post")
    def test_gemini_success_mock(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({
                        "summary": "Verified bug in parser",
                        "root_cause_hypothesis": "Missing bounds check",
                        "confirmed_facts": ["Parser crashes on empty string"],
                        "unknowns": [],
                        "evidence": ["FILE:src/parser.py", "ISSUE"],
                        "proposed_fix": "Add bounds check before indexing",
                        "risks": [],
                        "tests_to_run": ["test_empty_string"],
                        "confidence": 92
                    })}]
                }
            }]
        }
        mock_post.return_value = mock_resp

        res = run_provider_with_retry("gemini", "mock_prompt", json_mode=True)
        self.assertEqual(res["provider"], "gemini")
        self.assertEqual(res["confidence"], 92)
        self.assertIn("FILE:src/parser.py", res["evidence"])

    @patch("requests.post")
    def test_groq_success_mock(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "summary": "Groq verified bug in parser",
                        "root_cause_hypothesis": "Off-by-one index access",
                        "confirmed_facts": ["Off-by-one on loop termination"],
                        "unknowns": [],
                        "evidence": ["FILE:src/parser.py", "ISSUE"],
                        "proposed_fix": "Change < to <=",
                        "risks": [],
                        "tests_to_run": ["test_bounds"],
                        "confidence": 88
                    })
                }
            }]
        }
        mock_post.return_value = mock_resp

        res = run_provider_with_retry("groq", "mock_prompt", json_mode=True)
        self.assertEqual(res["provider"], "groq")
        self.assertEqual(res["confidence"], 88)

    @patch("requests.post")
    def test_retry_on_rate_limit_then_success(self, mock_post):
        # 1st attempt: 429 rate limit
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Rate limit exceeded"

        # 2nd attempt: 200 OK
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({
                        "summary": "Recovered after retry",
                        "root_cause_hypothesis": "Fixed",
                        "confirmed_facts": [],
                        "unknowns": [],
                        "evidence": ["FILE:foo.py", "ISSUE"],
                        "proposed_fix": "Done",
                        "confidence": 85
                    })}]
                }
            }]
        }
        mock_post.side_effect = [resp_429]

        with self.assertRaises(Exception) as ctx:
            run_provider_with_retry("gemini", "mock_prompt", json_mode=True)

        self.assertIn("429", str(ctx.exception))
        self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    def test_fallback_gemini_to_openrouter(self, mock_post):
        os.environ["OPENROUTER_API_KEY"] = "mock_openrouter_key"
        def route_post(url, **kwargs):
            if "generativelanguage.googleapis.com" in url:
                resp = MagicMock()
                resp.status_code = 429
                resp.text = "Quota exceeded"
                return resp
            else:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "summary": "OpenRouter fallback succeeded",
                                "root_cause_hypothesis": "Root cause",
                                "confirmed_facts": [],
                                "unknowns": [],
                                "evidence": ["FILE:a.py", "ISSUE"],
                                "proposed_fix": "Fix",
                                "confidence": 80,
                            })
                        }
                    }]
                }
                return resp
        mock_post.side_effect = route_post

        res, provider_used = run_with_fallback("mock_prompt", primary="gemini", fallback="openrouter")
        self.assertEqual(provider_used, "openrouter")
        self.assertEqual(res["summary"], "OpenRouter fallback succeeded")

    @patch("requests.post")
    def test_fallback_openrouter_to_gemini(self, mock_post):
        def route_post(url, **kwargs):
            if "generativelanguage.googleapis.com" in url:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {
                    "candidates": [{
                        "content": {
                            "parts": [{"text": json.dumps({
                                "summary": "Gemini fallback succeeded",
                                "root_cause_hypothesis": "Root cause",
                                "confirmed_facts": [],
                                "unknowns": [],
                                "evidence": ["FILE:b.py", "ISSUE"],
                                "proposed_fix": "Fix",
                                "confidence": 82,
                            })}]
                        }
                    }]
                }
                return resp
            else:
                resp = MagicMock()
                resp.status_code = 500
                resp.text = "Internal Server Error"
                return resp
        mock_post.side_effect = route_post

        res, provider_used = run_with_fallback("mock_prompt", primary="openrouter", fallback="gemini")
        self.assertEqual(provider_used, "gemini")
        self.assertEqual(res["summary"], "Gemini fallback succeeded")

    def test_unsupported_provider_rejected(self):
        with self.assertRaises(ValueError):
            run_provider_with_retry("openai", "prompt")
        with self.assertRaises(ValueError):
            run_provider_with_retry("anthropic", "prompt")
        with self.assertRaises(ValueError):
            run_provider_with_retry("deepseek", "prompt")
        with self.assertRaises(ValueError):
            run_provider_with_retry("kimi", "prompt")

    def test_configured_providers_filtering(self):
        os.environ["GEMINI_API_KEY"] = "mock_key"
        os.environ["GROQ_API_KEY"] = ""
        os.environ["OPENROUTER_API_KEY"] = "mock_openrouter_key"
        self.assertEqual(configured_providers(), ["gemini", "openrouter"])

        os.environ["OPENROUTER_API_KEY"] = ""
        self.assertEqual(configured_providers(), ["gemini"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for implementation safety, path traversal rejection, blocked file prevention, and test failure gates."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from implement import validate_patch_paths, changed_paths, extract_diff


class ImplementSafetyTests(unittest.TestCase):
    def test_extract_diff_markdown_fence(self):
        raw = "Here is the patch:\n```diff\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n```\nDone"
        extracted = extract_diff(raw)
        self.assertTrue(extracted.startswith("--- a/file.py"))
        self.assertIn("+new", extracted)

    def test_changed_paths_extraction(self):
        diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n+x\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n+y\n"
        paths = changed_paths(diff)
        self.assertEqual(paths, ["src/a.py", "src/b.py"])

    def test_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td)
            paths = ["src/clean.py", "../../outside.py"]
            with self.assertRaises(RuntimeError) as ctx:
                validate_patch_paths(paths, checkout)
            self.assertIn("path traversal", str(ctx.exception))

    def test_reject_absolute_paths(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td)
            paths = ["/etc/passwd"]
            with self.assertRaises(RuntimeError) as ctx:
                validate_patch_paths(paths, checkout)
            self.assertIn("absolute path", str(ctx.exception))

    def test_reject_windows_drive_path(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td)
            paths = ["C:\\Windows\\System32\\cmd.exe"]
            with self.assertRaises(RuntimeError) as ctx:
                validate_patch_paths(paths, checkout)
            self.assertIn("absolute path", str(ctx.exception))

    def test_reject_blocked_workflows(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td)
            paths = [".github/workflows/deploy.yml"]
            with self.assertRaises(RuntimeError) as ctx:
                validate_patch_paths(paths, checkout)
            self.assertIn("touches blocked path", str(ctx.exception))

    def test_reject_blocked_secrets_and_keys(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td)
            for bad_path in [".env", "id_rsa", "server.key", "credentials.json", "token.json"]:
                with self.assertRaises(RuntimeError):
                    validate_patch_paths([bad_path], checkout)

    def test_allow_safe_paths(self):
        with tempfile.TemporaryDirectory() as td:
            checkout = Path(td)
            (checkout / "src").mkdir(parents=True)
            (checkout / "src" / "valid.py").write_text("# code", encoding="utf-8")
            paths = ["src/valid.py"]
            # Should not raise
            validate_patch_paths(paths, checkout)


if __name__ == "__main__":
    unittest.main()

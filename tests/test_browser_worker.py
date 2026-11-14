"""Focused checks for the browser action boundary."""

import unittest

from daytona_worker.browser_worker import validate_action


class BrowserActionValidationTests(unittest.TestCase):
    def test_accepts_supported_action(self):
        self.assertEqual(
            validate_action({"tool": "open_page", "arguments": {"url": "https://example.com"}}),
            ("open_page", {"url": "https://example.com"}),
        )

    def test_rejects_unknown_tool(self):
        with self.assertRaises(ValueError):
            validate_action({"tool": "shell", "arguments": {"command": "echo hello"}})

    def test_rejects_extra_arguments(self):
        with self.assertRaises(ValueError):
            validate_action({"tool": "read_page", "arguments": {"script": "alert(1)"}})

    def test_rejects_invalid_type(self):
        with self.assertRaises(ValueError):
            validate_action({"tool": "fill", "arguments": {"label": "Search", "value": 123}})


if __name__ == "__main__":
    unittest.main()

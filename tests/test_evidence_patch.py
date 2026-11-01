import unittest

from gemini_web2api import tools


class EvidencePatchTests(unittest.TestCase):
    def test_path_seen_matches_suffix(self):
        self.assertTrue(tools._path_seen("server.py", {"/abs/pkg/server.py"}))
        self.assertTrue(tools._path_seen("pkg/server.py", {"server.py"}))
        self.assertFalse(tools._path_seen("server.py", {"/abs/pkg/myserver.py"}))

    def test_numbered_body_not_renumbered(self):
        body = "\n".join(f"{i:05d}| line number {i}" for i in range(1, 9))
        out = tools._format_file_body("a.py", body)
        self.assertEqual(out, f"--- Start a.py ---\n{body}\n--- End a.py ---")

    def test_plain_body_is_numbered(self):
        out = tools._format_file_body("a.py", "x = 1\ny = 2")
        self.assertIn("   1 | x = 1", out)

    def test_unverified_quotes(self):
        src = "\n".join(f"    value_{i} = compute_something_long(argument_{i})"
                        for i in range(40))
        msgs = [{"role": "user", "content": "review"},
                {"role": "tool", "tool_call_id": "c1", "content": src}]
        real = "```python\n" + "\n".join(
            f"    value_{i} = compute_something_long(argument_{i})"
            for i in (3, 7, 9)) + "\n```"
        fake = "```python\n" + "\n".join(
            f"    value_{i} = invented_function_call_here(arg_{i})"
            for i in (3, 7, 9)) + "\n```"
        self.assertEqual(tools.unverified_quotes(real, msgs), [])
        self.assertEqual(len(tools.unverified_quotes(fake, msgs)), 3)


if __name__ == "__main__":
    unittest.main()

import json
import unittest

from gemini_web2api import flow_patch as F


def _call(i, name, args, result):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": f"c{i}", "content": result},
    ]


class ToolKind(unittest.TestCase):
    def test_kinds(self):
        for name, kind in [("read", "read"), ("glob", "survey"), ("grep", "search"),
                           ("edit", "act"), ("write", "act"), ("bash", "exec"),
                           ("todowrite", "other"), ("webfetch", "other")]:
            self.assertEqual(F.tool_kind(name), kind, name)


class Listing(unittest.TestCase):
    def test_windows_drive_kept(self):
        msgs = [{"role": "user", "content": "review source"}]
        msgs += _call(0, "glob", {"pattern": "**/*.js"},
                      "D:\\Project\\x\\index.js\nD:\\Project\\x\\api\\s.js")
        out = F._listing_files(F._answered_calls(msgs))
        self.assertTrue(all(p.startswith("D:/") for p in out), out)


class Chunks(unittest.TestCase):
    def test_grouping_and_split(self):
        body = "x = 1\n" * 100
        files = [(f"f{i}.py", body) for i in range(5)]
        chunks = F._make_chunks(files, 10 ** 6, 2)
        self.assertEqual([len(c) for c in chunks], [2, 2, 1])
        big = [("big.py", "line\n" * 1000)]
        parts = F._make_chunks(big, 1000, 4)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all("part" in c[0][0] for c in parts))


class Verify(unittest.TestCase):
    chunk = [("a.py", "def f():\n    x = risky_call(None)\n")]

    def test_real_quote_kept_fake_dropped(self):
        txt = ("- a.py:2 -- `x = risky_call(None)` -- crash\n"
               "- a.py:9 -- `invented code line` -- bogus")
        kept, dropped = F._verify_findings(txt, self.chunk)
        self.assertEqual((len(kept), dropped), (1, 1))

    def test_none(self):
        self.assertEqual(F._verify_findings("NONE", self.chunk), ([], 0))

    def test_no_quote_dropped(self):
        kept, dropped = F._verify_findings("- a.py:2 -- looks bad", self.chunk)
        self.assertEqual((kept, dropped), ([], 1))


class Materials(unittest.TestCase):
    def test_failed_read_not_counted(self):
        msgs = [{"role": "user", "content": "review source"}]
        msgs += _call(0, "read", {"path": "a.py"}, "def f(): pass")
        msgs += _call(1, "read", {"path": "b.py"}, "Error: File not found: b.py")
        files, _ = F._review_materials(msgs)
        self.assertEqual([p for p, _ in files], ["a.py"])


if __name__ == "__main__":
    unittest.main()

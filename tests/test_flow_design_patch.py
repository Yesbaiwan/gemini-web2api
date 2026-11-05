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


def _tc(name, args):
    return {"id": "z", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _perm_edit(text, pattern):
    return F._ff5_permissions([{"role": "user", "content": text}], pattern).edit


class Guidance(unittest.TestCase):
    def test_how_to_inside_implement_request_is_not_guidance(self):
        self.assertTrue(_perm_edit(
            "Implement the retry in server.py so users know how to configure it",
            "CHANGE"))

    def test_real_how_question_is_guidance(self):
        self.assertFalse(_perm_edit("How can I fix the retry bug in server.py?",
                                    "CHANGE"))

    def test_no_pattern_can_edit(self):
        self.assertTrue(_perm_edit("update the config", None))

    def test_explicit_no_edit_wins_without_pattern(self):
        self.assertFalse(_perm_edit("do not edit my files", None))


class NewFile(unittest.TestCase):
    def _base(self):
        msgs = [{"role": "user", "content": "fix retry in server.py"}]
        return msgs + _call(0, "read", {"filePath": "server.py"}, "def retry(): pass")

    def test_new_test_file_allowed_after_not_found_read(self):
        msgs = self._base() + _call(
            1, "read", {"filePath": "tests/test_retry.py"},
            "Error: File not found: tests/test_retry.py")
        ctx = F._analyze(msgs)
        tc = _tc("write", {"filePath": "tests/test_retry.py", "content": "x"})
        self.assertEqual(F._ff5_act_needs_read_problems([tc], msgs, ctx), [])
        self.assertEqual(F._ff5_scope_problems([tc], msgs, ctx.perm), [])

    def test_edit_of_unread_file_still_blocked(self):
        msgs = self._base()
        ctx = F._analyze(msgs)
        tc = _tc("edit", {"filePath": "other.py", "oldString": "a", "newString": "b"})
        self.assertEqual(len(F._ff5_act_needs_read_problems([tc], msgs, ctx)), 1)

    def test_write_over_listed_unread_file_blocked(self):
        msgs = [{"role": "user", "content": "fix retry in server.py"}]
        msgs += _call(0, "glob", {"pattern": "**/*.py"}, "server.py\nutil.py")
        ctx = F._analyze(msgs)
        tc = _tc("write", {"filePath": "util.py", "content": "x = 1"})
        self.assertEqual(len(F._ff5_act_needs_read_problems([tc], msgs, ctx)), 1)


if __name__ == "__main__":
    unittest.main()

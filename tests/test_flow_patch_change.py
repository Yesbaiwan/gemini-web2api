import json
import unittest

from gemini_web2api import flow_patch as F

FILE_TEXT = "1: a = 1\n2: def f():\n3:     return 1\n4: b = 2\n5: c = 3"


def _call(i, name, args, result):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": f"c{i}", "content": result},
    ]


def _tm():
    props = {
        "read": {"filePath"},
        "edit": {"filePath", "oldString", "newString"},
        "bash": {"command", "description"},
        "glob": {"pattern"},
        "grep": {"pattern"},
    }
    req = {"read": {"filePath"}, "bash": {"command"},
           "edit": {"filePath", "oldString", "newString"}}
    return F.ToolMap(set(props), props, req)


def _base(request="fix the retry bug in server.py"):
    msgs = [{"role": "user", "content": request}]
    msgs += _call(0, "read", {"filePath": "server.py"}, "def retry(): pass")
    return msgs


class TestsAfterNamedFile(unittest.TestCase):
    def test_tests_step_runs_without_survey(self):
        msgs = _base() + _call(1, "edit", {"filePath": "server.py"},
                               "Edit applied successfully.")
        step = F.plan_for(msgs, _tm())
        self.assertEqual(step.name, "tests")
        cmd = json.loads(step.calls[0]["function"]["arguments"])["command"]
        self.assertIn("unittest", cmd)


class FailedEdit(unittest.TestCase):
    def test_failed_edit_returns_to_act_with_hint(self):
        msgs = _base() + _call(1, "edit", {"filePath": "server.py"},
                               "Error: oldString not found in content")
        step = F.plan_for(msgs, _tm())
        self.assertEqual(step.name, "act")
        self.assertIn("FAILED", step.instruction)

    def test_cap_stops_the_loop(self):
        msgs = _base()
        for i in range(1, 7):
            msgs += _call(i, "edit", {"filePath": "server.py"},
                          "Error: oldString not found in content")
        step = F.plan_for(msgs, _tm())
        self.assertEqual(step.name, "answer")
        self.assertIn("budget", step.instruction)


def _edit_tc(old, new):
    return {"id": "x", "type": "function", "function": {
        "name": "edit", "arguments": json.dumps(
            {"filePath": "a.py", "oldString": old, "newString": new})}}


class EditGate(unittest.TestCase):
    def _msgs(self):
        m = [{"role": "user", "content": "fix a.py"}]
        return m + _call(0, "read", {"filePath": "a.py"}, FILE_TEXT)

    def test_strips_line_number_prefix(self):
        calls, problems = F._gate_act_calls(
            [_edit_tc("3:     return 1", "3:     return 2")], self._msgs())
        self.assertEqual(problems, [])
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["oldString"], "    return 1")
        self.assertEqual(args["newString"], "    return 2")

    def test_exact_text_untouched(self):
        calls, problems = F._gate_act_calls(
            [_edit_tc("    return 1", "    return 2")], self._msgs())
        self.assertEqual(problems, [])
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["oldString"], "    return 1")

    def test_mismatch_reports_closest_line(self):
        _calls, problems = F._gate_act_calls(
            [_edit_tc("def g():", "def h():")], self._msgs())
        self.assertEqual(len(problems), 1)
        self.assertIn("def f():", problems[0])

    def test_unread_file_is_not_checked(self):
        msgs = [{"role": "user", "content": "fix a.py"}]
        calls, problems = F._gate_act_calls(
            [_edit_tc("anything", "x")], msgs)
        self.assertEqual((len(calls), problems), (1, []))


class ChangeSearchFirst(unittest.TestCase):
    def test_grep_then_rank_by_hits(self):
        msgs = [{"role": "user", "content": "fix the `retry_delay` handling"}]
        msgs += _call(0, "glob", {"pattern": "**/*.py"}, "pkg/a.py\npkg/b.py")
        step = F.plan_for(msgs, _tm())
        self.assertEqual(step.name, "search")
        msgs += _call(1, "grep", {"pattern": "x"},
                      "pkg/b.py:10: retry_delay = 1\npkg/b.py:20: retry_delay")
        step = F.plan_for(msgs, _tm())
        self.assertEqual(step.name, "read")
        self.assertIn("pkg/b.py", step.instruction)


if __name__ == "__main__":
    unittest.main()

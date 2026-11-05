import unittest

from gemini_web2api import flow_patch as F


def _u(text):
    return {"role": "user", "content": text}


RUNNY = ("RUN_COMMAND", "RUN_TESTS", "GIT_INFO")


class PermissionsWithoutPattern(unittest.TestCase):
    def test_no_pattern_can_edit(self):
        self.assertTrue(F._ff5_permissions([_u("update the config")], None).edit)

    def test_no_pattern_still_honours_explicit_ban(self):
        self.assertFalse(F._ff5_permissions([_u("do not edit my files")], None).edit)


class NegatedRun(unittest.TestCase):
    def test_vietnamese_negated_command_is_not_run(self):
        ctx = F._analyze([_u("Đừng chạy `git reset --hard`, chỉ giải thích thôi")])
        self.assertNotIn(ctx.pattern, RUNNY)

    def test_english_negated_tests_are_not_run(self):
        ctx = F._analyze([_u("please don't run the tests")])
        self.assertNotIn(ctx.pattern, RUNNY)

    def test_plain_request_still_runs_tests(self):
        ctx = F._analyze([_u("run the tests")])
        self.assertEqual(ctx.pattern, "RUN_TESTS")


import json


def _call(i, name, args, result):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c%d" % i, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "c%d" % i, "content": result},
    ]


def _tm():
    props = {"read": {"filePath", "offset", "limit"},
             "edit": {"filePath", "oldString", "newString"},
             "bash": {"command", "description"},
             "glob": {"pattern"}, "grep": {"pattern"}}
    req = {"read": {"filePath"}, "bash": {"command"},
           "edit": {"filePath", "oldString", "newString"}}
    return F.ToolMap(set(props), props, req)


def _edit(old, new, path="a.py"):
    return {"id": "z", "type": "function", "function": {
        "name": "edit", "arguments": json.dumps(
            {"filePath": path, "oldString": old, "newString": new})}}


class GateJoinsReads(unittest.TestCase):
    def test_continued_read_keeps_first_part(self):
        msgs = [_u("fix a.py")]
        msgs += _call(0, "read", {"filePath": "a.py"},
                      "alpha = 1\nbeta = 2\n[Output truncated. Continue reading with offset: 100]")
        msgs += _call(1, "read", {"filePath": "a.py", "offset": 100}, "gamma = 3")
        _c, problems = F._gate_act_calls([_edit("alpha = 1", "alpha = 9")], msgs)
        self.assertEqual(problems, [])

    def test_edit_of_edited_text_is_not_checked_against_stale_read(self):
        msgs = [_u("fix a.py")]
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1\ny = 2")
        msgs += _call(1, "edit", {"filePath": "a.py", "oldString": "x = 1",
                                  "newString": "x = 22"}, "Edit applied successfully.")
        _c, problems = F._gate_act_calls([_edit("x = 22", "x = 23")], msgs)
        self.assertEqual(problems, [])

    def test_real_mismatch_still_reported(self):
        msgs = [_u("fix a.py")] + _call(0, "read", {"filePath": "a.py"}, "x = 1\ny = 2")
        _c, problems = F._gate_act_calls([_edit("z = 3", "z = 4")], msgs)
        self.assertEqual(len(problems), 1)


class SupersededFailedRead(unittest.TestCase):
    def test_failed_then_successful_read_does_not_block(self):
        msgs = [_u("fix server.py")]
        msgs += _call(0, "read", {"filePath": "server.py"}, "Error: file not found")
        msgs += _call(1, "read", {"filePath": "pkg/server.py"}, "x = 1")
        self.assertIsNone(F._ff5_read_guard(F._analyze(msgs), msgs))

    def test_only_failed_read_still_blocks(self):
        msgs = [_u("fix server.py")]
        msgs += _call(0, "read", {"filePath": "server.py"}, "Error: file not found")
        self.assertTrue(F._ff5_read_guard(F._analyze(msgs), msgs))


class BareNameSurvey(unittest.TestCase):
    def test_survey_before_reading_a_bare_name(self):
        msgs = [_u("fix retry in server.py")]
        self.assertEqual(F.plan_for(msgs, _tm()).name, "survey")

    def test_nested_file_is_read_after_the_listing(self):
        msgs = [_u("fix retry in server.py")]
        msgs += _call(0, "glob", {"pattern": "**/*.py"}, "pkg/server.py\npkg/util.py")
        step = F.plan_for(msgs, _tm())
        self.assertEqual(step.name, "read")
        args = json.loads(step.calls[0]["function"]["arguments"])
        self.assertEqual(args["filePath"], "pkg/server.py")


class ReReadAfterFailedTests(unittest.TestCase):
    def test_reread_before_the_next_edit(self):
        msgs = [_u("fix a.py")]
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1")
        msgs += _call(1, "edit", {"filePath": "a.py", "oldString": "x = 1",
                                  "newString": "x = 2"}, "Edit applied successfully.")
        msgs += _call(2, "bash", {"command": "python -m unittest discover -s tests"},
                      "Ran 1 test in 0.001s\n\nFAILED (failures=1)")
        step = F.plan_for(msgs, _tm())
        self.assertEqual(step.name, "read")
        args = json.loads(step.calls[0]["function"]["arguments"])
        self.assertEqual(args["filePath"], "a.py")
        msgs += _call(3, "read", {"filePath": "a.py"}, "x = 2")
        self.assertEqual(F.plan_for(msgs, _tm()).name, "act")


class TruncationMarkers(unittest.TestCase):
    def test_both_formats(self):
        rx = F._FF4_TRUNC_RE
        self.assertEqual(rx.findall("x\n[Output truncated. Continue reading with offset: 100]"), ["100"])
        self.assertEqual(rx.findall("x\n(Showing lines 1-2000 of 3500. Use offset=2001 to continue.)"), ["2001"])
        self.assertEqual(rx.sub("", "a (Showing lines 1-9 of 99. Use offset=10 to continue.)"), "a ")


if __name__ == "__main__":
    unittest.main()

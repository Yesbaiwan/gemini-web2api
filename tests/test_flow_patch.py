import json
import re
import unittest
from unittest import mock

import gemini_web2api.config as config
from gemini_web2api.config import CONFIG
import gemini_web2api.flow_patch as F
import gemini_web2api.multimodal as M
from gemini_web2api import tools
import gemini_web2api.tools as T

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
RUNNY = ("RUN_COMMAND", "RUN_TESTS", "GIT_INFO")
FILE_TEXT = "1: a = 1\n2: def f():\n3:     return 1\n4: b = 2\n5: c = 3"

def _call(i, name, args, result):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}" if isinstance(i, int) else str(i), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": f"c{i}" if isinstance(i, int) else str(i), "content": result},
    ]

def _u(text):
    return {"role": "user", "content": text}

def _tc(name, args):
    return {"id": "z", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}

def _edit_tc(old, new):
    return {"id": "x", "type": "function", "function": {
        "name": "edit", "arguments": json.dumps(
            {"filePath": "a.py", "oldString": old, "newString": new})}}

def _edit(old, new, path="a.py"):
    return {"id": "z", "type": "function", "function": {
        "name": "edit", "arguments": json.dumps(
            {"filePath": path, "oldString": old, "newString": new})}}

def _tm(read_props=None):
    props = {
        "read": set(read_props or {"filePath", "offset", "limit"}),
        "edit": {"filePath", "oldString", "newString"},
        "write": {"filePath", "content"},
        "bash": {"command", "description"},
        "glob": {"pattern"},
        "grep": {"pattern"},
    }
    req = {"read": {"filePath"}, "bash": {"command"},
           "edit": {"filePath", "oldString", "newString"},
           "write": {"filePath", "content"}}
    return F.ToolMap(set(props), props, req)

def _ctx(**kw):
    c = F._Ctx()
    for k, v in kw.items():
        setattr(c, k, v)
    return c

def _perm_edit(text, pattern):
    return F._ff5_permissions([{"role": "user", "content": text}], pattern).edit

def _public(host):
    return "93.184.216.34" if host == "img.example.com" else None

def _wrb(text):
    inner = json.dumps([0, 0, 0, 0, [[None, [text]]]], ensure_ascii=False)
    line = json.dumps([["wrb.fr", None, inner]], ensure_ascii=False)
    return line + " " * max(0, 250 - len(line))

def _base(request="fix the retry bug in server.py"):
    msgs = [{"role": "user", "content": request}]
    msgs += _call(0, "read", {"filePath": "server.py"}, "def retry(): pass")
    return msgs


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

class TestSummary(unittest.TestCase):
    def test_only_summary_lines(self):
        out = ("  \u2714 retry sau rename fail 1 l\u1ea7n -> ok (65ms)\n"
               "\u2139 tests 178\n\u2139 pass 178\n\u2139 fail 0\n\u2139 duration_ms 62944.3\n")
        msgs = [{"role": "user", "content": "review source"}]
        msgs += _call(0, "shell", {"command": "npm test"}, out)
        _f, tests = F._review_materials(msgs)
        self.assertEqual(F._test_summary(tests),
                         ["tests 178", "pass 178", "fail 0"])

class TestsAfterNamedFile(unittest.TestCase):
    def test_tests_step_runs_without_survey(self):
        msgs = _base() + _call(1, "edit", {"filePath": "server.py"},
                               "Edit applied successfully.")
        step = F.plan_for(msgs, _tm())
        # Proposal 2: the diff stage sits between the edit and verification.
        self.assertEqual(step.name, "diff")
        cmd = json.loads(step.calls[0]["function"]["arguments"])["command"]
        self.assertIn("git", cmd)
        self.assertIn("diff", cmd)
        msgs += _call(2, "bash", {"command": cmd}, "diff --git a/server.py b/server.py")
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

class Permissions(unittest.TestCase):
    def test_run_has_three_levels(self):
        p = F._ff5_permissions(
            [{"role": "user", "content": "fix a.py but do not run any commands"}],
            "CHANGE")
        self.assertTrue(p.edit)
        self.assertEqual(p.run, "none")

        p = F._ff5_permissions([{"role": "user", "content": "fix a.py"}], "CHANGE")
        self.assertEqual(p.run, "any")

        p = F._ff5_permissions([{"role": "user", "content": "review a.py"}],
                               "FILE_REVIEW")
        self.assertEqual(p.run, "readonly")

    def test_guidance_never_grants_write(self):
        ctx = F._analyze([{"role": "user",
                           "content": "how can I fix a.py?"}])
        self.assertEqual(ctx.pattern, "EXPLAIN")
        self.assertFalse(ctx.perm.edit)
        self.assertEqual(ctx.perm.run, "readonly")

    def test_no_edit_is_sticky(self):
        msgs = [{"role": "user", "content": "do not edit my files"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "fix a.py"}]
        p = F._ff5_permissions(msgs, "CHANGE")
        self.assertFalse(p.edit)

class Scope(unittest.TestCase):
    def test_named_file_limits_writes(self):
        msgs = [{"role": "user", "content": "fix a.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1")
        ctx = F._analyze(msgs)
        self.assertEqual(ctx.perm.scope, ["a.py"])
        self.assertTrue(F._ff5_in_scope("a.py", ctx.perm.scope))
        self.assertFalse(F._ff5_in_scope("b.py", ctx.perm.scope))

        tc = {"id": "z", "type": "function", "function": {
            "name": "edit", "arguments": json.dumps(
                {"filePath": "b.py", "oldString": "x", "newString": "y"})}}
        problems = F._ff5_scope_problems([tc], msgs, ctx.perm)
        self.assertEqual(len(problems), 1)
        self.assertIn("b.py", problems[0])

    def test_create_target_is_in_scope(self):
        ctx = _ctx(pattern="CREATE", scannable="create a module", target="pkg/new.py")
        self.assertIn("pkg/new.py", F._ff5_scope(ctx, []))

class EvidenceStatus(unittest.TestCase):
    def _e(self, result):
        return {"kind": "read", "args": {"filePath": "a.py"}, "result": result}

    def test_status_levels(self):
        self.assertEqual(F._ff5_status(self._e("")), "unknown")
        self.assertEqual(F._ff5_status(self._e("Error: file not found")), "failed")
        self.assertEqual(F._ff5_status(self._e("x = 1")), "success")
        trunc = "x = 1\n[... output truncated, continue at offset: 100]"
        self.assertEqual(F._ff5_status(self._e(trunc)), "partial")

class EvidenceGate(unittest.TestCase):
    def test_truncated_read_is_continued(self):
        msgs = [{"role": "user", "content": "explain a.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"},
                      "1: x = 1\n[... output truncated, continue at offset: 100]")
        ctx = F._analyze(msgs)
        step, stop = F._ff5_evidence_gate(ctx, msgs, _tm())
        self.assertIsNone(stop)
        self.assertEqual(step.name, "read")
        args = json.loads(step.calls[0]["function"]["arguments"])
        self.assertEqual(args["offset"], 100)

    def test_empty_read_is_retried_once_then_stopped(self):
        msgs = [{"role": "user", "content": "explain a.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"}, "")
        ctx = F._analyze(msgs)
        step, stop = F._ff5_evidence_gate(ctx, msgs, _tm())
        self.assertIsNone(stop)
        self.assertEqual(step.name, "read")

        msgs += _call(1, "read", {"filePath": "a.py"}, "")
        step, stop = F._ff5_evidence_gate(ctx, msgs, _tm())
        self.assertIsNone(step)
        self.assertIn("stopped", stop)

    def test_failed_read_blocks_an_answer(self):
        msgs = [{"role": "user", "content": "explain a.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"}, "Error: file not found")
        ctx = F._analyze(msgs)
        step, stop = F._ff5_evidence_gate(ctx, msgs, _tm())
        self.assertIsNone(step)
        self.assertIn("could not read", stop)

class Adapt(unittest.TestCase):
    def test_alias_normalisation_and_unknown_key_drop(self):
        call = {"id": "x", "type": "function", "function": {
            "name": "read", "arguments": json.dumps(
                {"file_path": "a.py", "start_line": 10, "bogus": 1})}}
        calls, problems = F._ff5_adapt_calls([call], _tm())
        self.assertEqual(problems, [])
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["filePath"], "a.py")
        self.assertEqual(args["offset"], 10)
        self.assertNotIn("bogus", args)
        self.assertNotIn("file_path", args)

    def test_required_parameter_is_checked_before_emit(self):
        call = {"id": "x", "type": "function", "function": {
            "name": "edit", "arguments": json.dumps(
                {"filePath": "a.py", "oldString": "x"})}}
        _calls, problems = F._ff5_adapt_calls([call], _tm())
        self.assertEqual(len(problems), 1)
        self.assertIn("newString", problems[0])

    def test_no_schema_keeps_the_key_the_model_used(self):
        tm = F.ToolMap({"read"}, {"read": set()}, {})
        call = {"id": "x", "type": "function", "function": {
            "name": "read", "arguments": json.dumps({"file_path": "a.py"})}}
        calls, problems = F._ff5_adapt_calls([call], tm)
        self.assertEqual(problems, [])
        self.assertIn("file_path",
                      json.loads(calls[0]["function"]["arguments"]))

class DiffStage(unittest.TestCase):
    def test_diff_is_read_after_a_successful_edit(self):
        msgs = [{"role": "user", "content": "fix a.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1")
        msgs += _call(1, "edit", {"filePath": "a.py", "oldString": "x = 1",
                                  "newString": "x = 2"}, "Edit applied successfully.")
        ctx = F._analyze(msgs)
        step = F.Step("CHANGE", "tests", 1, 1, {"exec"}, "run tests", [], True,
                      ["bash"])
        diff = F._ff5_diff_step(ctx, step, msgs, _tm())
        self.assertIsNotNone(diff)
        self.assertEqual(diff.name, "diff")
        cmd = json.loads(diff.calls[0]["function"]["arguments"])["command"]
        self.assertIn("git", cmd)
        self.assertIn("diff", cmd)

    def test_diff_is_not_repeated(self):
        msgs = [{"role": "user", "content": "fix a.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1")
        msgs += _call(1, "edit", {"filePath": "a.py", "oldString": "x = 1",
                                  "newString": "x = 2"}, "Edit applied successfully.")
        msgs += _call(2, "bash", {"command": "git diff -- a.py"}, "diff --git ...")
        ctx = F._analyze(msgs)
        step = F.Step("CHANGE", "tests", 1, 1, {"exec"}, "run tests", [], True,
                      ["bash"])
        self.assertIsNone(F._ff5_diff_step(ctx, step, msgs, _tm()))

class CrossModule(unittest.TestCase):
    def test_missing_import_between_read_files(self):
        msgs = [{"role": "user", "content": "review a.py and b.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"},
                      "def foo(x):\n    return x\n")
        msgs += _call(1, "read", {"filePath": "b.py"},
                      "from a import bar\nbar(1)\n")
        findings = F._ff5_cross_module(msgs)
        self.assertEqual(len(findings), 1)
        self.assertIn("bar", findings[0])
        self.assertIn("a.py", findings[0])

    def test_wrong_arity_is_reported(self):
        msgs = [{"role": "user", "content": "review a.py and b.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"},
                      "def foo(x, y):\n    return x\n")
        msgs += _call(1, "read", {"filePath": "b.py"},
                      "from a import foo\nfoo(1)\n")
        findings = F._ff5_cross_module(msgs)
        self.assertTrue(any("at least 2" in f for f in findings), findings)

class Banner(unittest.TestCase):
    def test_banner_is_generated_from_the_spec(self):
        text = F._ff5_spec_banner([
            {"role": "user", "content": "review source code and find bugs"}])
        self.assertIn("GENERIC_REVIEW", text)
        self.assertIn("Mandatory flow", text)

    def test_no_edit_request_does_not_announce_an_edit(self):
        text = F._ff5_spec_banner([
            {"role": "user",
             "content": "how can I fix the retry bug? do not edit my files"}])
        self.assertIn("EXPLAIN", text)
        self.assertIn("no file edits", text)
        self.assertNotIn("-> edit", text)

    def test_install_sets_the_single_banner_hook(self):
        from gemini_web2api import tools as T
        old = getattr(T, "_TASK_BANNER_HOOK", None)
        try:
            F._ff5_install_banner()
            self.assertIs(T._TASK_BANNER_HOOK, F._ff5_spec_banner)
            text = T.task_pattern_banner([
                {"role": "user", "content": "review source code and find bugs"}])
            self.assertIn("GENERIC_REVIEW", text)
            self.assertIn("Mandatory flow", text)
        finally:
            T._TASK_BANNER_HOOK = old

class Postfilter(unittest.TestCase):
    def _ctx_messages(self, text="fix a.py"):
        msgs = [{"role": "user", "content": text}]
        return msgs + _call(0, "read", {"filePath": "a.py"}, "x = 1")

    def test_run_none_blocks_any_command(self):
        msgs = self._ctx_messages("fix a.py but do not run any commands")
        perm = F._analyze(msgs).perm
        tc = {"id": "z", "type": "function", "function": {
            "name": "bash", "arguments": json.dumps({"command": "ls"})}}
        result, nudge = F._ff5_postfilter(("", [tc]), msgs, perm,
                                          last=True, tm=_tm())
        text, calls = result
        self.assertIsNone(calls)
        self.assertIn("Running commands is not allowed", text)
        self.assertIsNone(nudge)

    def test_out_of_scope_write_is_rejected(self):
        msgs = self._ctx_messages()
        perm = F._analyze(msgs).perm
        tc = {"id": "z", "type": "function", "function": {
            "name": "edit", "arguments": json.dumps(
                {"filePath": "b.py", "oldString": "x", "newString": "y"})}}
        result, nudge = F._ff5_postfilter(("", [tc]), msgs, perm,
                                          last=False, tm=_tm())
        self.assertIsNone(result)
        self.assertIn("outside this request", nudge)

    def test_schema_mismatch_names_the_missing_parameter(self):
        msgs = self._ctx_messages()
        perm = F._analyze(msgs).perm
        tc = {"id": "z", "type": "function", "function": {
            "name": "edit", "arguments": json.dumps(
                {"filePath": "a.py", "oldString": "x"})}}
        result, nudge = F._ff5_postfilter(("", [tc]), msgs, perm,
                                          last=False, tm=_tm())
        self.assertIsNone(result)
        self.assertIn("newString", nudge)

    def test_readonly_mode_blocks_a_writing_command(self):
        msgs = [{"role": "user", "content": "review a.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1")
        perm = F._analyze(msgs).perm
        tc = {"id": "z", "type": "function", "function": {
            "name": "bash", "arguments": json.dumps({"command": "rm a.py"})}}
        result, nudge = F._ff5_postfilter(("", [tc]), msgs, perm,
                                          last=False, tm=_tm())
        self.assertIsNone(result)
        self.assertIn("not read-only", nudge)

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

class ContinuedRead(unittest.TestCase):
    def test_finished_truncation_does_not_block_edit(self):
        msgs = [{"role": "user",
                 "content": "Implement thêm endpoint v1/health redirect google"}]
        msgs += _call(0, "read", {"filePath": "server.py"},
                      "1: a\n[Output truncated. Continue reading with offset: 100]")
        msgs += _call(1, "read", {"filePath": "server.py", "offset": 100}, "100: b")
        ctx = F._analyze(msgs)
        self.assertEqual(ctx.pattern, "CHANGE")
        tm = F.ToolMap({"read"}, {"read": {"filePath", "offset", "limit"}}, {})
        _step, stop = F._ff5_evidence_gate(ctx, msgs, tm)
        self.assertIsNone(stop)

    def test_truncated_read_with_limit_retries_with_larger_limit(self):
        msgs = [{"role": "user",
                 "content": "Implement thêm endpoint v1/health redirect google"}]
        msgs += _call(0, "read", {"filePath": "server.py", "limit": 2000},
                      "1: a\n[Output truncated. Continue reading with offset: 100]")
        ctx = F._analyze(msgs)
        tm = F.ToolMap({"read"}, {"read": {"filePath", "limit"}}, {})
        step, stop = F._ff5_evidence_gate(ctx, msgs, tm)
        self.assertIsNone(stop)
        self.assertEqual(step.name, "read")
        args = json.loads(step.calls[0]["function"]["arguments"])
        self.assertEqual(args["filePath"], "server.py")
        self.assertGreaterEqual(args["limit"], 4000)

    def test_truncated_read_without_offset_or_limit_stops(self):
        msgs = [{"role": "user",
                 "content": "Implement thêm endpoint v1/health redirect google"}]
        msgs += _call(0, "read", {"filePath": "server.py"},
                      "1: a\n[Output truncated. Continue reading with offset: 100]")
        ctx = F._analyze(msgs)
        tm = F.ToolMap({"read"}, {"read": {"filePath"}}, {})
        step, stop = F._ff5_evidence_gate(ctx, msgs, tm)
        self.assertIsNone(step)
        self.assertTrue(stop)
        self.assertIn("offset", stop)
        self.assertIn("limit", stop)

    def test_deleted_file_after_truncation_stops_without_reissuing(self):
        msgs = [{"role": "user",
                 "content": "Implement thêm endpoint v1/health redirect google"}]
        msgs += _call(0, "read", {"filePath": "server.py"},
                      "1: a\n[Output truncated. Continue reading with offset: 100]")
        msgs += _call(1, "read", {"filePath": "server.py", "offset": 100},
                      "Error: file not found")
        ctx = F._analyze(msgs)
        self.assertEqual(
            [p for _i, p, _n in F._ff4_unfinished(F._answered_calls(msgs))],
            ["server.py"])
        tm = F.ToolMap({"read"}, {"read": {"filePath", "offset"}}, {})
        step, stop = F._ff5_evidence_gate(ctx, msgs, tm)
        self.assertIsNone(step)
        self.assertTrue(stop)
        self.assertTrue("deleted" in stop or "xóa" in stop)
        self.assertIn("file not found", stop)

    def test_filter_keeps_not_found_read_of_truncated_path(self):
        msgs = [{"role": "user",
                 "content": "Implement thêm endpoint v1/health redirect google"}]
        msgs += _call(0, "read", {"filePath": "server.py"},
                      "1: a\n[Output truncated. Continue reading with offset: 100]")
        msgs += _call(1, "read", {"filePath": "server.py", "offset": 100},
                      "Error: file not found")
        ctx = F._analyze(msgs)
        filtered = F._ff6c_filter(ctx, msgs)
        self.assertEqual(len(filtered), len(msgs))

    def test_failed_continuation_does_not_finish_truncation(self):
        msgs = [{"role": "user",
                 "content": "Implement thêm endpoint v1/health redirect google"}]
        msgs += _call(0, "read", {"filePath": "server.py"},
                      "1: a\n[Output truncated. Continue reading with offset: 100]")
        msgs += _call(1, "read", {"filePath": "server.py", "offset": 100},
                      "Error: cannot read server.py")
        ctx = F._analyze(msgs)
        self.assertEqual(
            [p for _i, p, _n in F._ff4_unfinished(F._answered_calls(msgs))],
            ["server.py"])
        tm = F.ToolMap({"read"}, {"read": {"filePath"}}, {})
        _step, stop = F._ff5_evidence_gate(ctx, msgs, tm)
        self.assertTrue(stop)

    def test_named_create_file_is_still_create(self):
        ctx = F._analyze([{"role": "user",
                           "content": "create util.py with a helper"}])
        self.assertEqual(ctx.pattern, "CREATE")
        self.assertEqual(ctx.target, "util.py")

class FetchImage(unittest.TestCase):
    def _fetch(self, hops, url="http://img.example.com/a.png"):
        with mock.patch.object(M, "_resolve_public_ip", side_effect=_public), \
             mock.patch.object(M, "_ssrf7_hop", side_effect=hops) as hop, \
             mock.patch.dict(CONFIG, {"proxy": None}):
            out = M.fetch_image_bytes(url)
        return out, hop

    def test_redirect_to_private_blocked(self):
        out, hop = self._fetch([(302, "http://127.0.0.1/secret", b"")])
        self.assertEqual(out, b"")
        self.assertEqual(hop.call_count, 1)

    def test_public_redirect_followed(self):
        out, hop = self._fetch([(302, "/b.png", b""), (200, None, PNG)])
        self.assertEqual(out, PNG)
        self.assertEqual(hop.call_count, 2)

    def test_non_image_rejected(self):
        out, _ = self._fetch([(200, None, b"<html>nope</html>")])
        self.assertEqual(out, b"")

    def test_redirect_loop_capped(self):
        out, hop = self._fetch([(302, "/x", b"")] * 10)
        self.assertEqual(out, b"")
        self.assertLessEqual(hop.call_count, 6)

class ListingTwins(unittest.TestCase):
    def test_relative_same_name_kept(self):
        ev = [{"kind": "survey", "args": {}, "name": "glob",
               "result": "server.py\npkg/server.py"}]
        out = F._listing_files(ev)
        self.assertIn("server.py", out)
        self.assertIn("pkg/server.py", out)

    def test_truncated_twin_dropped(self):
        ev = [{"kind": "survey", "args": {}, "name": "glob",
               "result": "D:\\Project\\x\\README.md\nProject/x/README.md"}]
        out = F._listing_files(ev)
        self.assertEqual(out, ["D:/Project/x/README.md"])

class GooglePseudo(unittest.TestCase):
    def test_tool_result_stays_in_order(self):
        req = {"contents": [
            {"role": "user", "parts": [{"text": "a"}]},
            {"role": "model", "parts": [{"functionCall": {
                "name": "read", "args": {"path": "x.py"}}}]},
            {"role": "user", "parts": [{"functionResponse": {
                "name": "read", "response": {"c": "1"}}}]},
            {"role": "user", "parts": [{"text": "b"}]},
        ]}
        roles = [m["role"] for m in T._google_pseudo_messages(req)]
        self.assertEqual(roles, ["user", "assistant", "tool", "user"])
        msgs = T._google_pseudo_messages(req)
        self.assertEqual(msgs[1]["tool_calls"][0]["id"], msgs[2]["tool_call_id"])

class ReadonlyShell(unittest.TestCase):
    def test_newline_is_rejected(self):
        self.assertFalse(F._ff5_cmd_readonly("git status\nrm a.py"))
        self.assertFalse(F._ff5_cmd_readonly("git status\r\nrm a.py"))

    def test_plain_commands_still_pass(self):
        for c in ("git status --short", "ls && git diff", "git --no-pager log -n 5",
                  "python -m unittest discover -s tests", "cat a.py | head -20",
                  "git status 2>&1", "ls >/dev/null", "git branch -a",
                  "git branch --list", "git remote -v", "find . -name x.py"):
            self.assertTrue(F._ff5_cmd_readonly(c), c)

    def test_unsupported_syntax_is_rejected(self):
        for c in ("ls & rm a.py", "echo $(rm a.py)", "ls `rm a.py`",
                  "find . -delete", "find . -exec rm {} ;", "git branch -D x",
                  "git branch newbranch", "git diff --output=x", "sort -o a b",
                  "git status > out.txt", "git -c core.pager=x log",
                  "git remote add o url", "ls >/dev/null.txt", "rm a.py"):
            self.assertFalse(F._ff5_cmd_readonly(c), c)

    def test_postfilter_blocks_the_newline_trick(self):
        msgs = [_u("review a.py")] + _call(0, "read", {"filePath": "a.py"}, "x = 1")
        perm = F._analyze(msgs).perm
        self.assertEqual(perm.run, "readonly")
        tc = _tc("bash", {"command": "git status\nrm a.py"})
        result, nudge = F._ff5_postfilter(("", [tc]), msgs, perm, last=False, tm=_tm())
        self.assertIsNone(result)
        self.assertIn("not read-only", nudge)

class Grants(unittest.TestCase):
    def test_edit_grant_keeps_the_command_ban(self):
        msgs = [_u("do not run any command"), {"role": "assistant", "content": "ok"},
                _u("you may edit a.py now")]
        p = F._ff5_permissions(msgs, "CHANGE")
        self.assertTrue(p.edit)
        self.assertEqual(p.run, "none")

    def test_run_grant_lifts_only_the_command_ban(self):
        msgs = [_u("do not edit my files and do not run any commands"),
                _u("you may run the tests now")]
        p = F._ff5_permissions(msgs, "CHANGE")
        self.assertFalse(p.edit)
        self.assertEqual(p.run, "any")

    def test_generic_go_ahead_grants_edit_only(self):
        msgs = [_u("do not run any commands"), _u("go ahead")]
        p = F._ff5_permissions(msgs, "CHANGE")
        self.assertTrue(p.edit)
        self.assertEqual(p.run, "none")

class AbsenceProof(unittest.TestCase):
    def test_extension_limited_glob_proves_nothing(self):
        msgs = [_u("create pkg/new.py")] + _call(
            0, "glob", {"pattern": "**/*.{py,js}"}, "pkg/a.py\npkg/b.py")
        self.assertFalse(F._ff8_proven_absent("pkg/new.py", F._answered_calls(msgs)))

    def test_complete_listing_proves_absence_and_presence(self):
        msgs = [_u("create pkg/new.py")] + _call(
            0, "glob", {"pattern": "**/*"}, "pkg/a.py\npkg/b.py")
        ev = F._answered_calls(msgs)
        self.assertTrue(F._ff8_proven_absent("pkg/new.py", ev))
        self.assertFalse(F._ff8_proven_absent("pkg/a.py", ev))
        self.assertFalse(F._ff8_proven_absent(".env", ev))

    def test_long_listing_may_be_capped(self):
        body = "\n".join("pkg/f%d.py" % i for i in range(150))
        msgs = [_u("create pkg/new.py")] + _call(0, "glob", {"pattern": "**/*"}, body)
        self.assertFalse(F._ff8_proven_absent("pkg/new.py", F._answered_calls(msgs)))

    def test_not_found_read_is_proof(self):
        msgs = [_u("create new.py")] + _call(
            0, "read", {"filePath": "new.py"}, "Error: File not found: new.py")
        self.assertTrue(F._ff8_proven_absent("new.py", F._answered_calls(msgs)))

    def test_existing_keeps_files_missing_from_a_partial_listing(self):
        msgs = [_u("fix pkg/x.py")]
        F._ff8_tls.messages = msgs
        try:
            self.assertEqual(F._existing(["pkg/x.py"], ["pkg/a.py"]), ["pkg/x.py"])
        finally:
            F._ff8_tls.messages = None

    def test_create_probes_the_target_before_writing(self):
        ctx = F._Ctx()
        ctx.pattern, ctx.target, ctx.create_merge = "CREATE", "pkg/new.py", False
        act = F.Step("CREATE", "act", 3, 5, {"act"}, "write it", None, False, ["write"])
        msgs = [_u("create pkg/new.py")]
        with mock.patch.object(F, "_ff8_prev_plan_create", return_value=act):
            step = F._plan_create(ctx, F._answered_calls(msgs), _tm())
        self.assertEqual(step.name, "read")
        msgs += _call(0, "read", {"filePath": "pkg/new.py"}, "Error: File not found")
        with mock.patch.object(F, "_ff8_prev_plan_create", return_value=act):
            step = F._plan_create(ctx, F._answered_calls(msgs), _tm())
        self.assertEqual(step.name, "act")

class Regate(unittest.TestCase):
    def _msgs(self):
        body = "x = 1\n" * 100
        msgs = [_u("fix a.py")]
        msgs += _call(0, "read", {"filePath": "a.py"}, body)
        msgs += _call(1, "edit", {"filePath": "a.py", "oldString": "x = 1",
                                  "newString": "x = 2"}, "Edit applied successfully.")
        msgs += _call(2, "read", {"filePath": "a.py"}, body)
        return msgs

    def test_shrinking_rewrite_after_fresh_read_is_blocked(self):
        msgs = self._msgs()
        ctx = F._analyze(msgs)
        tc = _tc("write", {"filePath": "a.py", "content": "x = 1"})
        result, nudge = F._ff5_postfilter(("", [tc]), msgs, ctx.perm, last=True,
                                          tm=_tm(), ctx=ctx)
        self.assertIsNone(nudge)
        self.assertIsNone(result[1])
        result, nudge = F._ff5_postfilter(("", [tc]), msgs, ctx.perm, last=False,
                                          tm=_tm(), ctx=ctx)
        self.assertIsNone(result)
        self.assertIn("rejected", nudge)

    def test_a_sane_second_edit_still_passes(self):
        msgs = self._msgs()
        ctx = F._analyze(msgs)
        tc = _tc("edit", {"filePath": "a.py", "oldString": "x = 1",
                          "newString": "x = 3"})
        result, nudge = F._ff5_postfilter(("", [tc]), msgs, ctx.perm, last=True,
                                          tm=_tm(), ctx=ctx)
        self.assertIsNone(nudge)
        self.assertTrue(result[1])

class LeanSource(unittest.TestCase):
    def setUp(self):
        self.orig = dict(CONFIG)
        CONFIG["flow6_lean_prompt"] = True

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.orig)

    def _ctx(self, request):
        ctx = F._Ctx()
        ctx.pattern, ctx.last, ctx.request, ctx.fix = "CHANGE", request, request, False
        perm = F._Ctx()
        perm.edit, perm.run, perm.scope = True, "any", ["a.py"]
        ctx.perm = perm
        return ctx

    def test_prompt_keeps_numbered_source(self):
        msgs = [_u("fix a.py")] + _call(0, "read", {"filePath": "a.py"},
                                       "def f():\n    return 1\n")
        step = F.Step("CHANGE", "act", 3, 6, {"act"}, "Edit a.py now.",
                      tool_names=["edit"])
        p = F.build_step_prompt(self._ctx("fix a.py"), step, msgs, _tm())
        self.assertIn("def f():", p)
        self.assertIn("   1 | def f():", p)
        self.assertIn("WITHOUT the", p)

    def test_big_file_keeps_the_relevant_window(self):
        lines = ["x_%d = %d" % (i, i) for i in range(2000)]
        lines[1500] = "def target_function(): pass"
        msgs = [_u("fix target_function in a.py")] + _call(
            0, "read", {"filePath": "a.py"}, "\n".join(lines))
        CONFIG["flow8_lean_source_chars"] = 3000
        step = F.Step("CHANGE", "act", 3, 6, {"act"}, "Edit a.py now.",
                      tool_names=["edit"])
        p = F.build_step_prompt(self._ctx("fix target_function in a.py"),
                                step, msgs, _tm())
        self.assertIn("def target_function(): pass", p)
        self.assertIn("lines omitted", p)
        self.assertLess(len(p), 6000)

    def test_read_stage_gets_no_source(self):
        msgs = [_u("fix a.py")] + _call(0, "read", {"filePath": "a.py"}, "x = 1\n")
        step = F.Step("CHANGE", "read", 2, 6, {"read"}, "Read it.", tool_names=["read"])
        p = F.build_step_prompt(self._ctx("fix a.py"), step, msgs, _tm())
        self.assertNotIn("Source (code you read", p)

class BardFalsePositive(unittest.TestCase):
    def setUp(self):
        F._ff8_patch_gemini()

    def test_answer_quoting_the_error_name_is_not_a_rejection(self):
        from gemini_web2api import gemini as G
        raw = _wrb('The regex BardErrorInfo [1155] and BardErrorInfo",[1155] appear at line 340.')
        self.assertIsNone(G.bard_error_code(raw))
        self.assertTrue(G.extract_response_text(raw).startswith("The regex"))

    def test_name_followed_far_away_by_a_digit_is_not_a_rejection(self):
        from gemini_web2api import gemini as G
        raw = _wrb("BardErrorInfo is parsed here.\n\nSee line 12 for details. " * 5)
        self.assertIsNone(G.bard_error_code(raw))

    def test_real_rejections_are_still_detected(self):
        from gemini_web2api import gemini as G
        pb = ('[["wrb.fr",null,null,null,null,[13,null,'
              '[["type.googleapis.com/assistant.boq.bard.application.BardErrorInfo",'
              '[1155]]]]]]\n')
        prose = 'something\n[["wrb.fr",null,"... BardErrorInfo [1099] ..."]]\n'
        self.assertEqual(G.bard_error_code(pb), 1155)
        self.assertEqual(G.bard_error_code(prose), 1099)
        with self.assertRaises(G.GeminiError):
            G.extract_response_text(pb)

class DestructiveCommands(unittest.TestCase):
    def test_wide_forms_are_caught(self):
        for c in ("rm -fr build", "rm -r -f x", "rm --recursive --force x",
                  "rm -rf /", "git clean --force -d", "git clean -fd",
                  "git -C sub clean -fd", "git push --force", "git push origin -f",
                  "git checkout -f", "git checkout -- .", "git checkout .",
                  "git branch -D old", "git reset --hard HEAD~1",
                  "git stash drop", "find . -delete", "del /f /s /q x",
                  "git restore ."):
            self.assertTrue(F._FF5_DESTRUCTIVE_RE.search(c), c)

    def test_ordinary_commands_pass(self):
        for c in ("git status", "rm -f a.tmp", "git clean -n", "git push origin main",
                  "git checkout main", "git branch -d merged", "git reset --soft HEAD~1",
                  "python -m unittest discover", "ls -la", "git diff"):
            self.assertIsNone(F._FF5_DESTRUCTIVE_RE.search(c), c)

class RevertPermission(unittest.TestCase):
    def test_how_to_undo_is_not_a_revert(self):
        ctx = F._analyze([_u("how can I undo the changes made in this session?")])
        self.assertNotEqual(ctx.pattern, "REVERT")
        self.assertFalse(ctx.perm.edit)

    def test_do_not_edit_blocks_revert(self):
        msgs = [_u("do not edit my files"), {"role": "assistant", "content": "ok"},
                _u("revert all changes in this session")]
        ctx = F._analyze(msgs)
        self.assertNotEqual(ctx.pattern, "REVERT")

    def test_real_revert_request_is_kept(self):
        ctx = F._analyze([_u("revert all changes in this session")])
        self.assertEqual(ctx.pattern, "REVERT")
        self.assertTrue(ctx.perm.edit)

    def test_own_result_refuses_a_delete_without_edit_rights(self):
        ctx = F._Ctx()
        ctx.pattern, ctx.last = "REVERT", "undo"
        perm = F._Ctx()
        perm.edit, perm.run, perm.scope = False, "any", None
        call = _tc("bash", {"command": "rm -f -- \"a.py\""})
        step = F._Ff5Step("revert", calls=[call])
        text, calls = F._ff5_own_result(step, ctx, [_u("undo")], _tm(), perm)
        self.assertIsNone(calls)
        self.assertIn("nothing", text.lower())

class PermissionsWithoutPattern(unittest.TestCase):
    def test_no_pattern_can_edit(self):
        self.assertTrue(F._ff5_permissions([_u("update the config")], None).edit)

    def test_no_pattern_still_honours_explicit_ban(self):
        self.assertFalse(F._ff5_permissions([_u("do not edit my files")], None).edit)

    def test_fix_request_with_explicit_no_edit_demoted_to_explain(self):
        cases = [
            "fix the login bug but do not edit anything",
            "fix bug, no edits",
            "repair the parser bug, read only",
        ]
        for prompt in cases:
            with self.subTest(prompt=prompt):
                ctx = F._analyze([_u(prompt)])
                self.assertEqual("EXPLAIN", ctx.pattern)
                self.assertFalse(ctx.perm.edit)
                self.assertEqual("readonly", ctx.perm.run)
                self.assertIn("user forbade edits", ctx.perm.reason or "")

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

# ---------------------------------------------------------------------------
# flowfix10: ambiguity-aware pattern routing
#
# A weak review verb ("tim", "find", "check") plus a code noun ("source",
# "code") must not force the review flow when the user only asked to
# understand the code. The deterministic regex stays the cheap filter; a
# model tie-break is consulted only for weak review labels that conflict
# with EXPLAIN/CHANGE/TOOL_DEMAND. ``OTHER`` means no forced flow and
# ``AMBIGUOUS`` asks the client to pick through CLARIFY_MARK.
# ---------------------------------------------------------------------------

REPRO = ("giải thích phần xử lý tìm pattern flow. trường hợp regex ra nhiều "
         "pattern trùng thì source đang làm thế nào")


def _folded10(text):
    scannable = F._strip_code_spans(text)
    return re.sub(r"\s+", " ", F._fold_diacritics(scannable)).strip()


class _Ff10Case(unittest.TestCase):
    def setUp(self):
        self._saved_model = getattr(F._ff5_tls, "model", None)
        self._saved_flags = {
            k: CONFIG.get(k)
            for k in ("flow_tiebreak", "flow_ask_on_ambiguity",
                      "flow_ask_when_unclear")
        }
        F._ff10_cache.clear()
        F._ff5_cache.clear()

    def tearDown(self):
        F._ff5_tls.model = self._saved_model
        F._ff10_cache.clear()
        F._ff5_cache.clear()
        for key, value in self._saved_flags.items():
            if value is None:
                CONFIG.pop(key, None)
            else:
                CONFIG[key] = value

    def _with_model(self, reply):
        """Install a fake classifier model; return (patch, calls)."""
        F._ff5_tls.model = (7, 0, None, None)
        calls = []

        def fake_generate(prompt, *args, **kwargs):
            calls.append(prompt)
            return reply

        return mock.patch.object(F, "generate", fake_generate), calls


class Ff10LexicalDemotion(_Ff10Case):
    def test_explain_question_is_not_a_source_review(self):
        self.assertFalse(F._is_source_review(_folded10(REPRO)))

    def test_explain_question_has_no_deterministic_pattern(self):
        self.assertIsNone(F.detect_task_pattern([_u(REPRO)]))

    def test_explain_question_analyzes_as_explain(self):
        ctx = F._analyze([_u(REPRO)])
        self.assertEqual("EXPLAIN", ctx.pattern)

    def test_candidates_mark_the_weak_review_and_the_explain(self):
        cands = F._ff10_candidates([_u(REPRO)])
        self.assertEqual(["GENERIC_REVIEW", "EXPLAIN"],
                         [c["label"] for c in cands])

    def test_weak_review_plus_explain_is_ambiguous(self):
        self.assertTrue(F._ff10_ambiguous(F._ff10_candidates([_u(REPRO)])))

    def test_strong_review_is_not_demoted(self):
        self.assertTrue(F._is_source_review(
            _folded10("review the source code and report bugs")))
        self.assertEqual(
            "GENERIC_REVIEW",
            F.detect_task_pattern([_u("review source code and find bugs")]))

    def test_weak_review_without_explain_marker_still_reviews(self):
        self.assertTrue(F._is_source_review(_folded10("check the source code")))
        self.assertEqual(
            "GENERIC_REVIEW",
            F.detect_task_pattern([_u("check the source code")]))

    def test_weak_review_with_a_defect_noun_is_not_demoted(self):
        text = "giải thích giúp, source có bug ở phần tìm pattern không"
        self.assertTrue(F._is_source_review(_folded10(text)))

    def test_wrong_is_a_defect_noun(self):
        self.assertTrue(F._is_source_review(
            _folded10("find what is wrong in the source code")))
        self.assertEqual(
            "GENERIC_REVIEW",
            F.detect_task_pattern([_u("find what is wrong in the source code")]))

    def test_file_review_is_demoted_in_an_explain_question(self):
        text = "đọc server.py rồi giải thích flow xử lý"
        folded = _folded10(text)
        self.assertTrue(F._ff10_orig_file_review(folded, text))
        self.assertFalse(F._is_file_review(folded, text))


class Ff10ModelTieBreak(_Ff10Case):
    def test_model_can_override_the_weak_review(self):
        patch, calls = self._with_model("GENERIC_REVIEW")
        with patch:
            ctx = F._analyze([_u(REPRO)])
        self.assertEqual("GENERIC_REVIEW", ctx.pattern)
        self.assertEqual(1, len(calls))
        self.assertFalse(ctx.fix)

    def test_model_can_override_a_weak_review_to_explain(self):
        text = "check how the source handles errors"
        self.assertEqual("GENERIC_REVIEW", F._analyze([_u(text)]).pattern)
        patch, calls = self._with_model("EXPLAIN")
        with patch:
            ctx = F._analyze([_u(text)])
        self.assertEqual("EXPLAIN", ctx.pattern)
        self.assertEqual(1, len(calls))

    def test_other_means_free_run_and_no_banner(self):
        patch, _ = self._with_model("OTHER")
        with patch:
            ctx = F._analyze([_u(REPRO)])
            banner = F._ff5_spec_banner([_u(REPRO)])
        self.assertIsNone(ctx.pattern)
        self.assertTrue(ctx.free_run)
        self.assertFalse(ctx.ask_user)
        self.assertEqual("", banner)

    def test_ambiguous_asks_the_client(self):
        patch, _ = self._with_model("AMBIGUOUS")
        with patch:
            ctx = F._analyze([_u(REPRO)])
            text = F.clarification_text([_u(REPRO)])
        self.assertTrue(ctx.ask_user)
        self.assertIn(F.CLARIFY_MARK, text)

    def test_ambiguous_does_not_ask_when_the_flag_is_off(self):
        CONFIG["flow_ask_on_ambiguity"] = False
        patch, _ = self._with_model("AMBIGUOUS")
        with patch:
            ctx = F._analyze([_u(REPRO)])
            text = F.clarification_text([_u(REPRO)])
        self.assertFalse(getattr(ctx, "ask_user", False))
        self.assertIsNone(text)

    def test_strong_review_never_calls_the_model(self):
        patch, calls = self._with_model("EXPLAIN")
        with patch:
            ctx = F._analyze([_u("review source code and find bugs")])
        self.assertEqual("GENERIC_REVIEW", ctx.pattern)
        self.assertEqual([], calls)

    def test_tiebreak_can_be_disabled(self):
        CONFIG["flow_tiebreak"] = False
        patch, calls = self._with_model("GENERIC_REVIEW")
        with patch:
            ctx = F._analyze([_u(REPRO)])
        self.assertEqual("EXPLAIN", ctx.pattern)
        self.assertEqual([], calls)

    def test_unavailable_model_keeps_the_deterministic_pattern(self):
        F._ff5_tls.model = None
        with mock.patch.object(F, "generate",
                               side_effect=AssertionError("must not call")):
            ctx = F._analyze([_u(REPRO)])
        self.assertEqual("EXPLAIN", ctx.pattern)


class Ff10Regressions(_Ff10Case):
    def test_how_can_i_fix_stays_explain(self):
        self.assertEqual("EXPLAIN", F._analyze([_u("how can I fix a.py?")]).pattern)

    def test_fix_request_stays_change(self):
        self.assertEqual("CHANGE", F._analyze([_u("fix a.py")]).pattern)

    def test_file_review_is_not_weak(self):
        cands = F._ff10_candidates([_u("review server.py and find bugs")])
        self.assertEqual(["FILE_REVIEW"], [c["label"] for c in cands])
        self.assertFalse(cands[0]["weak"])
        self.assertEqual("FILE_REVIEW",
                         F.detect_task_pattern([_u("review server.py and find bugs")]))


class Ff10GrepPlannerPaths(unittest.TestCase):
    """Grep snippets must not become read targets (live session 11/5)."""

    GREP = (
        "Found 2 matches\n"
        + r"D:\Project\gemini-web2api\tests\test_flow_patch.py:" + "\n"
        + r'  Line 207: msgs += _call(0, "glob", {"pattern": "**/*.py"}, '
        + r'"pkg/a.py\npkg/b.py")' + "\n"
        + r"  Line 8: import gemini_web2api.flow_patch as F" + "\n"
    )

    def _msgs(self):
        return [_u(REPRO)] + _call(0, "grep", {"pattern": "pattern"}, self.GREP)

    def test_search_hits_only_keeps_header_paths(self):
        ev = F._answered_calls(self._msgs())
        self.assertEqual(
            {"D:/Project/gemini-web2api/tests/test_flow_patch.py": 1},
            F._search_hits(ev))

    def test_planned_read_uses_the_full_path(self):
        step = F.plan_for(self._msgs(), _tm())
        self.assertEqual("read", step.name, msg=getattr(step, "instruction", ""))
        paths = [json.loads(c["function"]["arguments"])["filePath"]
                 for c in (step.calls or [])]
        self.assertEqual(["D:/Project/gemini-web2api/tests/test_flow_patch.py"],
                         paths)

    def test_base_planner_uses_the_full_path(self):
        step = F._base_plan_for(self._msgs(), _tm())
        self.assertEqual("read", step.name, msg=getattr(step, "instruction", ""))
        paths = [json.loads(c["function"]["arguments"])["filePath"]
                 for c in (step.calls or [])]
        self.assertEqual(["D:/Project/gemini-web2api/tests/test_flow_patch.py"],
                         paths)


class TestFlowEngineEnhancements(unittest.TestCase):
    """Flow engine improvements:
    1. Post-test self-healing loop (allowing repair acts after max_acts exceeded).
    2. Static AST Pre-Gate (detecting UnboundLocalError and reentrant Lock deadlocks).
    3. Plan Compliance & Test Step evaluation (tracking all required test_* functions).
    4. Multi-stage targeted test ordering.
    """

    def setUp(self):
        self.orig_config = dict(CONFIG)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.orig_config)
        F._ff8_tls.messages = None

    def test_post_test_self_healing_allows_act_after_max_acts_exceeded(self):
        """When a CHANGE task with a plan has exceeded flow_max_acts (e.g. 7 acts),
        and tests fail with an error/traceback, _change_tail MUST return an 'act' step
        for self-healing instead of terminating with final=True answer step."""
        CONFIG["flow_max_acts"] = 3
        CONFIG["flow17_plan_max_acts"] = 12
        CONFIG["flow17_post_test_max_acts"] = 4

        plan_doc = (
            "# Feature Implementation Plan\n\n"
            "### Phase 1: Configuration\n"
            "- [ ] `cookie_pool`: list\n\n"
            "### Phase 2: State Manager\n"
            "- [ ] `init_cookie_pool()`: fn\n"
            "- [ ] `get_active_cookie_credentials()`: fn\n"
        )
        msgs = [
            {"role": "user", "content": "Implement cookie pool according to plan"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "c_r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
            {"role": "tool", "tool_call_id": "c_r2", "content": "# Section: gemini.py\n_current_pool_index = 0\n" + "x = 1\n" * 15000},
        ]
        # Simulate 7 successful act calls (exceeding flow_max_acts=3)
        for i in range(1, 8):
            msgs.extend([
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"c_a{i}", "type": "function", "function": {
                        "name": "edit",
                        "arguments": json.dumps({
                            "filePath": "gemini_web2api.py",
                            "oldString": f"x = {i}",
                            "newString": f"x = {i + 1}",
                        })
                    }}
                ]},
                {"role": "tool", "tool_call_id": f"c_a{i}", "content": "Edit applied successfully."},
            ])

        # Then test ran and FAILED with UnboundLocalError
        test_fail_output = (
            "Traceback (most recent call last):\n"
            '  File "tests/test_cookie_pool.py", line 42, in test_pool\n'
            "    get_active_cookie_credentials()\n"
            '  File "gemini_web2api.py", line 3995, in get_active_cookie_credentials\n'
            "    idx = _current_pool_index % n\n"
            "UnboundLocalError: cannot access local variable '_current_pool_index' where it is not associated with a value\n\n"
            "FAILED (errors=3)"
        )
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t1", "type": "function", "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": "python -m unittest discover -s tests"}),
                }}
            ]},
            {"role": "tool", "tool_call_id": "c_t1", "content": test_fail_output},
        ])

        tm = _tm()
        ctx = F._analyze(msgs)
        ev = F._answered_calls(msgs)
        step = F._change_tail("CHANGE", ctx, ev, tm, ["gemini_web2api.py", "tests/test_cookie_pool.py"], 5)

        self.assertIsNotNone(step)
        self.assertEqual("act", step.name)
        self.assertFalse(getattr(step, "final", False))
        self.assertIn("act", step.kinds)
        self.assertIn("FAILED", step.instruction)
        self.assertIn("UnboundLocalError", step.instruction)

    def test_static_ast_pre_gate_rejects_missing_global(self):
        """When an edit/write call assigns to a module-level variable without declaring
        'global <var>', _base_gate_act_calls must reject it immediately at the gate."""
        CONFIG["flow17_static_pre_gate"] = True
        hay = (
            "_current_pool_index = 0\n"
            "_pool_accounts = []\n"
            "def load_cookie():\n"
            "    pass\n"
        )
        messages = [
            {"role": "user", "content": "update cookie credentials"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}}
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": hay},
        ]
        bad_edit = {
            "id": "c_bad",
            "type": "function",
            "function": {
                "name": "edit",
                "arguments": json.dumps({
                    "filePath": "gemini_web2api.py",
                    "oldString": "def load_cookie():\n    pass",
                    "newString": (
                        "def get_active_cookie_credentials():\n"
                        "    idx = _current_pool_index % 2\n"
                        "    _current_pool_index = (idx + 1) % 2\n"
                        "    return ('a', 'b')\n"
                    ),
                }),
            },
        }

        out_calls, problems = F._base_gate_act_calls([bad_edit], messages)
        self.assertEqual([], out_calls)
        self.assertTrue(len(problems) > 0)
        self.assertTrue(any(
            "assigns to module-level variable '_current_pool_index' without declaring 'global _current_pool_index'" in p
            for p in problems
        ), problems)

    def test_static_ast_pre_gate_rejects_reentrant_deadlock(self):
        """When a call introduces nested lock acquisition on a non-reentrant threading.Lock(),
        _base_gate_act_calls must reject it to prevent deadlocks."""
        CONFIG["flow17_static_pre_gate"] = True
        hay = (
            "import threading\n"
            "_cookie_pool_lock = threading.Lock()\n"
            "def init_cookie_pool():\n"
            "    with _cookie_pool_lock:\n"
            "        pass\n"
        )
        messages = [
            {"role": "user", "content": "update pool"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}}
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": hay},
        ]
        deadlock_edit = {
            "id": "c_dl",
            "type": "function",
            "function": {
                "name": "edit",
                "arguments": json.dumps({
                    "filePath": "gemini_web2api.py",
                    "oldString": "def init_cookie_pool():",
                    "newString": (
                        "def get_active_cookie_credentials():\n"
                        "    with _cookie_pool_lock:\n"
                        "        init_cookie_pool()\n\n"
                        "def init_cookie_pool():"
                    ),
                }),
            },
        }
        out_calls, problems = F._base_gate_act_calls([deadlock_edit], messages)
        self.assertEqual([], out_calls)
        self.assertTrue(len(problems) > 0)
        self.assertTrue(any(
            "Potential deadlock detected in 'get_active_cookie_credentials': it holds '_cookie_pool_lock' and calls 'init_cookie_pool'" in p
            for p in problems
        ), problems)

    def test_plan_evaluation_keeps_test_step_pending_when_missing_test_case(self):
        """When the plan lists specific test_* functions in Phase 5, Phase 5 remains done=False
        until ALL of those test functions are defined in the test file."""
        plan_doc = (
            "# Plan\n\n"
            "### Phase 5: Verification & Unit Tests (`tests/test_cookie_pool.py`)\n"
            "- [ ] Tạo file test `tests/test_cookie_pool.py`:\n"
            "  1. `test_single_cookie_backward_compatibility`: desc\n"
            "  2. `test_pool_rotation_on_429`: desc\n"
            "  3. `test_pool_rotation_on_bard_1037`: desc\n"
            "  4. `test_pool_status_endpoint`: desc\n"
        )
        msgs = [
            {"role": "user", "content": "Implement plan"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}}
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_w1", "type": "function", "function": {
                    "name": "write",
                    "arguments": json.dumps({
                        "filePath": "tests/test_cookie_pool.py",
                        # Only defines 3 of the 4 required test cases (missing test_pool_status_endpoint)
                        "content": (
                            "import unittest\n"
                            "class T(unittest.TestCase):\n"
                            "    def test_single_cookie_backward_compatibility(self): pass\n"
                            "    def test_pool_rotation_on_429(self): pass\n"
                            "    def test_pool_rotation_on_bard_1037(self): pass\n"
                        ),
                    }),
                }}
            ]},
            {"role": "tool", "tool_call_id": "c_w1", "content": "Wrote file successfully."},
        ]

        ctx = F._analyze(msgs)
        ev = F._answered_calls(msgs)
        eval_steps = F._ff17_evaluate_plan_steps(ctx, ev, msgs)

        self.assertEqual(1, len(eval_steps))
        step5 = eval_steps[0]
        self.assertFalse(step5["done"])
        self.assertIn("test_pool_status_endpoint", step5["missing_tokens"])

        # Now model adds the missing test case
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_e2", "type": "function", "function": {
                    "name": "edit",
                    "arguments": json.dumps({
                        "filePath": "tests/test_cookie_pool.py",
                        "oldString": "    def test_pool_rotation_on_bard_1037(self): pass",
                        "newString": (
                            "    def test_pool_rotation_on_bard_1037(self): pass\n"
                            "    def test_pool_status_endpoint(self): pass"
                        ),
                    }),
                }}
            ]},
            {"role": "tool", "tool_call_id": "c_e2", "content": "Edit applied successfully."},
        ])
        ev2 = F._answered_calls(msgs)
        eval_steps2 = F._ff17_evaluate_plan_steps(ctx, ev2, msgs)
        self.assertEqual(1, len(eval_steps2))
        self.assertTrue(eval_steps2[0]["done"])
        self.assertEqual([], eval_steps2[0]["missing_tokens"])

    def test_multi_stage_test_verification_orders_targeted_first(self):
        """Targeted test files must be sorted before full discover suite in test verification commands."""
        plan_doc = (
            "## 4. Verification Commands\n"
            "```bash\n"
            "python -m unittest discover -s tests\n"
            "python -m unittest tests/test_cookie_pool.py\n"
            "```\n"
        )
        msgs = [
            {"role": "user", "content": "run tests according to feature_implementation_plan.md"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}}
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
        ]
        cmds = F._ff17_extract_verification_commands(msgs, require_existing=False)
        self.assertEqual(2, len(cmds))
        # Specific test file comes first
        self.assertIn("test_cookie_pool.py", cmds[0])
        # Full discover suite comes second
        self.assertIn("discover -s tests", cmds[1])

        # With require_existing=True (default), test files that do not exist yet are filtered out
        cmds_strict = F._ff17_extract_verification_commands(msgs)
        self.assertEqual(1, len(cmds_strict))
        self.assertIn("discover -s tests", cmds_strict[0])

    def test_plan_evaluation_short_circuits_audit_and_act_excludes_todowrite(self):
        """1. _ff17_evaluate_plan_steps short-circuits compliance audit when tests haven't completed.
        2. _ff5_after_plan does not include todowrite in step.tool_names for act step.
        """
        CONFIG["flow16_plan_todo"] = True
        CONFIG["flow17_plan_todo_act"] = True
        plan_doc = (
            "## 1. Implementation Steps\n"
            "- [ ] Step 1: Update code\n\n"
            "## 4. Verification Commands\n"
            "```bash\n"
            "python -m unittest discover -s tests\n"
            "```\n"
        )
        msgs = [
            {"role": "user", "content": "implement plan"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}}
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
        ]
        ctx = F._analyze(msgs)
        ev = F._answered_calls(msgs)

        # Audit should NOT be called because tests have not completed
        eval_steps = F._ff17_evaluate_plan_steps(ctx, ev, msgs)
        # eval_steps has 1 plan step + 2 appended steps (verification cmds + audit)
        self.assertEqual(3, len(eval_steps))
        self.assertIn("Chạy verification commands", eval_steps[1]["title"])
        self.assertFalse(eval_steps[1]["done"])
        self.assertEqual("Đối chiếu plan (compliance audit)", eval_steps[2]["title"])
        self.assertFalse(eval_steps[2]["done"])

        # Tool names for act must not include todowrite
        tm = F.ToolMap(
            {"edit", "todowrite"},
            {"edit": {"filePath", "oldString", "newString"}, "todowrite": {"todos"}},
            {"edit": {"filePath", "oldString", "newString"}, "todowrite": {"todos"}}
        )
        step_act = F.Step("CHANGE", "act", 3, 5, {"act"}, "do edit", None, False, ["edit"])
        out_step = F._ff5_after_plan(step_act, msgs, tm, ctx)
        self.assertNotIn("todowrite", out_step.tool_names)

        # 3. Lean step prompt is only used when flow6_lean_prompt is explicitly configured
        CONFIG["flow6_lean_prompt"] = False
        self.assertIsNone(F.build_step_prompt(ctx, step_act, msgs, tm))

    def test_static_ast_pre_gate_negative_tests_allow_valid_patterns(self):
        """Valid patterns that must NEVER be rejected by static AST pre-gate:
        1. Local variable shadowing module-level variable (data = [1]).
        2. Comprehensions ([x for x in xs]).
        3. Local variables starting with '_' (_cache = {}).
        """
        CONFIG["flow17_static_pre_gate"] = True
        hay = "data = {}\nx = 1\n_cache = {}\n"
        msgs = [{"role": "tool", "content": hay}]

        # 1. Local shadows module-level
        ed1 = [{
            "id": "c1", "type": "function", "function": {
                "name": "edit", "arguments": json.dumps({
                    "filePath": "gemini_web2api.py", "oldString": "",
                    "newString": "def process():\n    data = [1]\n    return data\n",
                })
            }
        }]
        out1, prob1 = F._base_gate_act_calls(ed1, msgs)
        self.assertEqual([], prob1)
        self.assertEqual(1, len(out1))

        # 2. Comprehension variable
        ed2 = [{
            "id": "c2", "type": "function", "function": {
                "name": "edit", "arguments": json.dumps({
                    "filePath": "gemini_web2api.py", "oldString": "",
                    "newString": "def transform(xs):\n    return [x for x in xs]\n",
                })
            }
        }]
        out2, prob2 = F._base_gate_act_calls(ed2, msgs)
        self.assertEqual([], prob2)
        self.assertEqual(1, len(out2))

        # 3. Local starting with '_'
        ed3 = [{
            "id": "c3", "type": "function", "function": {
                "name": "edit", "arguments": json.dumps({
                    "filePath": "gemini_web2api.py", "oldString": "",
                    "newString": "def cache_helper():\n    _cache = {}\n    return _cache\n",
                })
            }
        }]
        out3, prob3 = F._base_gate_act_calls(ed3, msgs)
        self.assertEqual([], prob3)
        self.assertEqual(1, len(out3))

    def test_static_ast_pre_gate_positive_test_rejects_read_before_assign(self):
        """Pattern _idx = _idx + 1 (or augassign _idx += 1) reads variable before write
        without global, and MUST be rejected as UnboundLocalError."""
        CONFIG["flow17_static_pre_gate"] = True
        hay = "_idx = 0\n"
        msgs = [{"role": "tool", "content": hay}]

        ed = [{
            "id": "c_bad", "type": "function", "function": {
                "name": "edit", "arguments": json.dumps({
                    "filePath": "gemini_web2api.py", "oldString": "",
                    "newString": "def increment():\n    _idx = _idx + 1\n    return _idx\n",
                })
            }
        }]
        out, prob = F._base_gate_act_calls(ed, msgs)
        self.assertEqual([], out)
        self.assertTrue(len(prob) > 0)
        self.assertTrue(any("UnboundLocalError" in p and "_idx" in p for p in prob), prob)

    def test_case_b_baseline_test_passed_does_not_skip_plan_steps(self):
        """When a baseline test runs and passes before any act, _change_tail must NOT enter
        Case B to re-run tests immediately after the first act; it must fall through to Case C
        to continue implementing the remaining plan phases."""
        CONFIG["flow_max_acts"] = 3
        CONFIG["flow17_plan_max_acts"] = 12

        plan_doc = (
            "# Feature Implementation Plan\n\n"
            "### Phase 1: Configuration\n"
            "- [ ] `cookie_pool`: list\n\n"
            "### Phase 2: State Manager\n"
            "- [ ] `init_cookie_pool()`: fn\n"
        )
        msgs = [
            {"role": "user", "content": "Implement cookie pool according to plan"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "c_r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
            {"role": "tool", "tool_call_id": "c_r2", "content": "# Section: gemini.py\nx = 1\n" * 5000},
            # Baseline test passed
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t0", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_flow_patch.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t0", "content": "Ran 100 tests in 1.0s\n\nOK"},
            # Phase 1 act
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_a1", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "gemini_web2api.py", "oldString": "x = 1", "newString": "cookie_pool = []\nx = 1"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_a1", "content": "Edit applied successfully."},
        ]

        tm = _tm()
        ctx = F._analyze(msgs)
        ev = F._answered_calls(msgs)
        F._ff8_tls.messages = msgs

        step = F._change_tail("CHANGE", ctx, ev, tm, ["gemini_web2api.py"], 5)
        self.assertIsNotNone(step)
        # MUST be an act step for the remaining Phase 2, NOT a tests step!
        self.assertEqual("act", step.name)
        self.assertIn("act", step.kinds)
        self.assertIn("Implement the plan steps", step.instruction)

    def test_plan_compliance_audit_arity_and_retry_loop(self):
        """Plan compliance audit checks:
        1. Tuple arity mismatch (e.g. 2-tuple vs expected 3-tuple).
        2. Headers or payload built outside retry loop (using OR condition).
        """
        B = chr(96)
        plan_doc = (
            f"# Feature Implementation Plan\n\n"
            f"### Phase 2: Credentials\n"
            f"- [ ] {B}get_active_cookie_credentials(){B}: trả về tuple {B}(cookie_str, sapisid, meta_dict){B}\n\n"
            f"### Phase 4: Retry Failover\n"
            f"Tự động retry failover khi gặp lỗi 429 hoặc Bard error.\n"
        )

        # Violation 1: Arity is 2-tuple instead of 3-tuple
        # Violation 2: _build_payload is inside loop, but _build_headers is outside loop
        code_with_violations = (
            "def get_active_cookie_credentials():\n"
            "    return ('cookie', 'sapisid')\n\n"
            "def generate():\n"
            "    headers = _build_headers()\n"
            "    for attempt in range(3):\n"
            "        payload = _build_payload()\n"
            "        call()\n"
        )
        msgs_viol = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "c_r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
            {"role": "tool", "tool_call_id": "c_r2", "content": code_with_violations},
        ]
        res_viol = F._ff17_audit_plan_compliance(msgs_viol, [])
        self.assertFalse(res_viol["compliant"])
        self.assertTrue(any("returns a 2-tuple, but the plan specifies a 3-tuple" in v for v in res_viol["violations"]))
        self.assertTrue(any("_build_headers" in v and "built outside the retry loop" in v for v in res_viol["violations"]))

        # Compliant version: 3-tuple returned and both inside retry loop
        code_compliant = (
            "def get_active_cookie_credentials():\n"
            "    return ('cookie', 'sapisid', {'auth_user': 0})\n\n"
            "def generate():\n"
            "    for attempt in range(3):\n"
            "        headers = _build_headers()\n"
            "        payload = _build_payload()\n"
            "        call()\n"
        )
        msgs_comp = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "c_r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
            {"role": "tool", "tool_call_id": "c_r2", "content": code_compliant},
        ]
        res_comp = F._ff17_audit_plan_compliance(msgs_comp, [])
        self.assertTrue(res_comp["compliant"])
        self.assertEqual([], res_comp["violations"])

    def test_two_parallel_edits_count_as_one_round(self):
        """Two parallel edits in the same turn must count as 1 repair round, not 2.
        After 2 repair turns (4 post-test acts), flow18 (max_repair_rounds=4) still allows round 3."""
        CONFIG["flow_max_acts"] = 10
        CONFIG["flow17_plan_max_acts"] = 10
        CONFIG["flow17_post_test_max_acts"] = 4
        CONFIG["flow18_enabled"] = True
        CONFIG["flow18_max_repair_rounds"] = 4
        CONFIG["flow18_hard_max_acts"] = 24

        plan_doc = (
            "# Plan\n"
            "- [x] `cookie_pool`: list\n"
            "- [x] `init_cookie_pool()`: fn\n"
        )
        msgs = [
            {"role": "user", "content": "Implement cookie pool"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "c_r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
            {"role": "tool", "tool_call_id": "c_r2", "content": "# Section: gemini.py\n" + "x = 1\n" * 15000},
        ]
        # Initial 10 implementation acts
        for i in range(1, 11):
            msgs.extend([
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"c_init{i}", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "gemini_web2api.py", "oldString": f"x = {i}", "newString": f"x = {i + 1}"})}},
                ]},
                {"role": "tool", "tool_call_id": f"c_init{i}", "content": "Edit applied."},
            ])
        # Test run 1 fails
        test_fail_1 = (
            "FAIL: test_pool_rotation (tests.test_cookie_pool.TestCookiePool)\n"
            "Traceback (most recent call last):\n"
            '  File "tests/test_cookie_pool.py", line 88, in test_pool_rotation\n'
            "    self.assertEqual(len(_pool_accounts), 2)\n"
            "AssertionError: 0 != 2\n\nFAILED (failures=1)"
        )
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_cookie_pool.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t1", "content": test_fail_1},
        ])
        # Round 1: Turn with 2 parallel edits
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_p1_a", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "gemini_web2api.py", "oldString": "x = 11", "newString": "x = 12"})}},
                {"id": "c_p1_b", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "gemini_web2api.py", "oldString": "x = 12", "newString": "x = 13"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_p1_a", "content": "Edit applied."},
            {"role": "tool", "tool_call_id": "c_p1_b", "content": "Edit applied."},
        ])
        # Test run 2 fails
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t2", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_cookie_pool.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t2", "content": test_fail_1},
        ])
        # Round 2: Turn with 2 parallel edits (total 4 post-test acts!)
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_p2_a", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "gemini_web2api.py", "oldString": "x = 13", "newString": "x = 14"})}},
                {"id": "c_p2_b", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "gemini_web2api.py", "oldString": "x = 14", "newString": "x = 15"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_p2_a", "content": "Edit applied."},
            {"role": "tool", "tool_call_id": "c_p2_b", "content": "Edit applied."},
        ])
        # Test run 3 fails
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t3", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_cookie_pool.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t3", "content": test_fail_1},
        ])

        tm = _tm()
        ctx = F._analyze(msgs)
        ev = F._answered_calls(msgs)
        F._ff8_tls.messages = msgs

        # Under flow18: rounds_used = 2 < 4. Must return 'act' step (not terminate)!
        step = F._change_tail("CHANGE", ctx, ev, tm, ["gemini_web2api.py", "tests/test_cookie_pool.py"], 5)
        self.assertIsNotNone(step)
        self.assertEqual("act", step.name)
        self.assertFalse(getattr(step, "final", False))

        # Under flow18_enabled=False: fallback to flow17 (post_test_acts = 4 >= 4) -> terminates!
        CONFIG["flow18_enabled"] = False
        step_fallback = F._change_tail("CHANGE", ctx, ev, tm, ["gemini_web2api.py", "tests/test_cookie_pool.py"], 5)
        self.assertIsNotNone(step_fallback)
        self.assertEqual("answer", step_fallback.name)
        self.assertIn("Edit budget exhausted", step_fallback.instruction)

    def test_same_failure_injects_no_progress_warning_and_source_windows(self):
        """When the same failure repeats across consecutive runs, _change_tail and _ff5_after_plan
        must inject an IDENTICAL failure warning and source code windows for tested symbols."""
        CONFIG["flow18_enabled"] = True
        CONFIG["flow18_max_repair_rounds"] = 4

        test_code = (
            "import unittest\n"
            "from gemini_web2api import init_cookie_pool, _pool_accounts\n"
            "class TestCookiePool(unittest.TestCase):\n"
            "    def test_pool_rotation(self):\n"
            "        init_cookie_pool([{'cookie': 'c1'}, {'cookie': 'c2'}])\n"
            "        self.assertEqual(len(_pool_accounts), 2)\n"
        )
        src_code = (
            "# Section: gemini.py\n"
            "_pool_accounts = []\n\n"
            "def init_cookie_pool(accounts):\n"
            "    global _pool_accounts\n"
            "    _pool_accounts = accounts\n"
            + "x = 1\n" * 15000
        )
        test_fail = (
            "FAIL: test_pool_rotation (tests.test_cookie_pool.TestCookiePool)\n"
            "Traceback (most recent call last):\n"
            '  File "tests/test_cookie_pool.py", line 6, in test_pool_rotation\n'
            "    self.assertEqual(len(_pool_accounts), 2)\n"
            "AssertionError: 0 != 2\n\nFAILED (failures=1)"
        )

        msgs = [
            {"role": "user", "content": "Fix cookie pool rotation"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "tests/test_cookie_pool.py"})}},
                {"id": "c_r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": test_code},
            {"role": "tool", "tool_call_id": "c_r2", "content": src_code},
            # Test run 1
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_cookie_pool.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t1", "content": test_fail},
            # Edit attempt
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_a1", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "gemini_web2api.py", "oldString": "x = 1", "newString": "x = 2"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_a1", "content": "Edit applied."},
            # Test run 2: IDENTICAL failure
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t2", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_cookie_pool.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t2", "content": test_fail},
        ]

        tm = _tm()
        ctx = F._analyze(msgs)
        ev = F._answered_calls(msgs)
        F._ff8_tls.messages = msgs

        step = F._change_tail("CHANGE", ctx, ev, tm, ["gemini_web2api.py", "tests/test_cookie_pool.py"], 5)
        self.assertIsNotNone(step)
        self.assertEqual("act", step.name)
        # Warning about identical output must be injected in instruction
        self.assertIn("IDENTICAL", step.instruction)
        self.assertIn("test_pool_rotation", step.instruction)

        # Context pack via _ff5_after_plan must inject source windows for init_cookie_pool / _pool_accounts
        step_with_pack = F._ff5_after_plan(step, msgs, tm, ctx)
        self.assertIn("[Diagnostic Source Windows for Failing Test Symbols", step_with_pack.instruction)
        self.assertIn("init_cookie_pool", step_with_pack.instruction)
        self.assertIn("_pool_accounts", step_with_pack.instruction)

    def test_progress_grants_bonus_round(self):
        """When a repair attempt reduces the number of failing tests, progress is recorded and a bonus round is granted."""
        CONFIG["flow18_enabled"] = True
        CONFIG["flow18_max_repair_rounds"] = 4
        CONFIG["flow18_progress_bonus_rounds"] = 2

        fail_2_tests = (
            "FAIL: test_first (tests.test_a.TestA)\n"
            "AssertionError: 1 != 2\n"
            "FAIL: test_second (tests.test_a.TestA)\n"
            "AssertionError: 3 != 4\n\nFAILED (failures=2)"
        )
        fail_1_test = (
            "FAIL: test_first (tests.test_a.TestA)\n"
            "AssertionError: 1 != 2\n\nFAILED (failures=1)"
        )

        msgs = [
            {"role": "user", "content": "Fix tests"},
            # Test 1: 2 failures
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_a.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t1", "content": fail_2_tests},
            # Edit
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_a1", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "a.py", "oldString": "a=1", "newString": "a=2"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_a1", "content": "Edit applied."},
            # Test 2: 1 failure (progress!)
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t2", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_a.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t2", "content": fail_1_test},
        ]
        ev = F._answered_calls(msgs)
        prog = F._ff18_analyze_failure_progression(ev)
        self.assertEqual(1, prog["progress_count"])
        self.assertEqual(1, prog["bonus_rounds"])
        self.assertFalse(prog["is_no_progress"])
        self.assertEqual(["test_first"], prog["failed_names"])

    def test_hard_cap_still_terminates(self):
        """When total acts reach flow18_hard_max_acts, _change_tail must terminate with final=True answer."""
        CONFIG["flow18_enabled"] = True
        CONFIG["flow18_hard_max_acts"] = 6
        CONFIG["flow18_max_repair_rounds"] = 10

        test_fail = "FAIL: test_x (test.TestX)\nAssertionError\nFAILED (failures=1)"
        msgs = [{"role": "user", "content": "Implement feature"}]
        # 6 acts
        for i in range(1, 7):
            msgs.extend([
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"c_a{i}", "type": "function", "function": {"name": "edit", "arguments": json.dumps({"filePath": "a.py", "oldString": f"x{i}", "newString": f"y{i}"})}},
                ]},
                {"role": "tool", "tool_call_id": f"c_a{i}", "content": "Done."},
            ])
        # Test failed
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_a.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t1", "content": test_fail},
        ])

        tm = _tm()
        ctx = F._analyze(msgs)
        ev = F._answered_calls(msgs)
        F._ff8_tls.messages = msgs

        step = F._change_tail("CHANGE", ctx, ev, tm, ["a.py"], 5)
        self.assertIsNotNone(step)
        self.assertEqual("answer", step.name)
        self.assertIn("Edit budget exhausted", step.instruction)

    def test_honest_final_rejects_false_completion_when_last_test_failed(self):
        """When the session ends with a failing test and model claims completion falsely:
        - On first attempt (last=False), _ff5_postfilter returns (None, nudge) requiring honest reporting.
        - On final attempt (last=True), _ff5_postfilter prepends warning notice.
        - When tests passed, _ff5_postfilter leaves the completion text untouched."""
        CONFIG["flow18_enabled"] = True
        CONFIG["flow18_honest_final"] = True

        test_fail = "FAIL: test_pool_rotation (TestCookiePool)\nAssertionError: 0 != 2\nFAILED (failures=1)"
        msgs_fail = [
            {"role": "user", "content": "Triển khai cookie pool"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_cookie_pool.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t1", "content": test_fail},
        ]

        false_text = "Các bước triển khai Cookie Pool Rotation đã được hoàn tất thành công 100%."
        res_tuple = (false_text, None)

        # 1. First attempt (last=False): must nudge for retry
        final_res, nudge = F._ff5_postfilter(res_tuple, msgs_fail, last=False)
        self.assertIsNone(final_res)
        self.assertIsNotNone(nudge)
        self.assertIn("CRITICAL REQUIREMENT", nudge)
        self.assertIn("test_pool_rotation", nudge)
        self.assertIn("still FAIL", nudge)

        # 2. Final attempt (last=True): prepends warning banner
        final_res2, nudge2 = F._ff5_postfilter(res_tuple, msgs_fail, last=True)
        self.assertIsNone(nudge2)
        self.assertIsNotNone(final_res2)
        text2, _ = final_res2
        self.assertTrue(text2.startswith("⚠️ [Lưu ý:"))
        self.assertIn("test_pool_rotation", text2)
        self.assertIn(false_text, text2)

        # 3. When last test passed: completion text is untouched
        test_pass = "Ran 10 tests in 0.5s\n\nOK"
        msgs_pass = [
            {"role": "user", "content": "Triển khai cookie pool"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_t1", "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": "python -m unittest tests/test_cookie_pool.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_t1", "content": test_pass},
        ]
        final_res3, nudge3 = F._ff5_postfilter(res_tuple, msgs_pass, last=False)
        self.assertIsNone(nudge3)
        self.assertEqual(res_tuple, final_res3)


class TestFlowFix19(unittest.TestCase):
    """Flow Engine 19 regression tests."""

    def test_ff19_self_call_gate_ignores_starred_args(self):
        """Star-unpacked calls like self.send_api_error(*_map_upstream_error(e)) must not trigger false positive arity errors."""
        src = '''
class GeminiHandler:
    def send_api_error(self, status, message, type_=None, code=None):
        pass

    def f(self, e):
        self.send_api_error(*_map_upstream_error(e))
'''
        probs = F._ff19_check_self_calls("gemini_web2api.py", src)
        self.assertEqual([], probs)

    def test_ff19_self_call_gate_catches_missing_path(self):
        """Calls with missing required arguments like self._needs_auth() when method expects (self, path) must be detected."""
        src = '''
class GeminiHandler:
    def _needs_auth(self, path):
        return True

    def do_GET(self):
        path = "/v1/cookies/pool"
        if self._needs_auth() and not self._authorized():
            return
'''
        probs = F._ff19_check_self_calls("gemini_web2api.py", src)
        self.assertTrue(len(probs) > 0)
        self.assertTrue(any("_needs_auth" in p and "expects 1-1" in p for p in probs), probs)

    def test_ff19_self_call_gate_scopes_methods_by_class(self):
        """A same-named method in another class must not lend its signature.

        Only the enclosing class's definition applies, so ``B.f(self)`` is
        valid even though ``A.f(self, x)`` requires an argument.
        """
        src = '''
class A:
    def f(self, x):
        return x

    def bad(self):
        self.f()

class B:
    def f(self):
        return 1

    def good(self):
        self.f()
'''
        probs = F._ff19_check_self_calls("gemini_web2api.py", src)
        self.assertEqual(1, len(probs), probs)
        self.assertIn("self.f() expects 1-1", probs[0])


if __name__ == "__main__":
    unittest.main()

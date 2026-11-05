import json
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

    def test_unfinished_truncation_without_offset_still_blocks_edit(self):
        msgs = [{"role": "user",
                 "content": "Implement thêm endpoint v1/health redirect google"}]
        msgs += _call(0, "read", {"filePath": "server.py"},
                      "1: a\n[Output truncated. Continue reading with offset: 100]")
        ctx = F._analyze(msgs)
        tm = F.ToolMap({"read"}, {"read": {"filePath", "limit"}}, {})
        _step, stop = F._ff5_evidence_gate(ctx, msgs, tm)
        self.assertTrue(stop)
        self.assertNotIn("read failed", stop)

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
        tm = F.ToolMap({"read"}, {"read": {"filePath", "limit"}}, {})
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

if __name__ == "__main__":
    unittest.main()

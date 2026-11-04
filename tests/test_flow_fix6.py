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


def _tm(read_props=None):
    props = {
        "read": set(read_props or {"filePath", "offset", "limit"}),
        "edit": {"filePath", "oldString", "newString"},
        "bash": {"command", "description"},
        "glob": {"pattern"},
        "grep": {"pattern"},
    }
    req = {"read": {"filePath"}, "bash": {"command"},
           "edit": {"filePath", "oldString", "newString"}}
    return F.ToolMap(set(props), props, req)


def _ctx(**kw):
    c = F._Ctx()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


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


if __name__ == "__main__":
    unittest.main()

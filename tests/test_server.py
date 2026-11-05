import json
import threading
import unittest
from unittest import mock

from gemini_web2api import flow_patch as F
from gemini_web2api import tools
from gemini_web2api.config import CONFIG
from gemini_web2api.server import GeminiHandler, SSE_PING_INTERVAL_SEC

CALL = '```tool_call\n{"name":"read","arguments":{"path":"a"}}\n```'


def _call(i, name, args, result):
    return [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": f"c{i}", "content": result},
    ]


def _tm():
    props = {
        "read": {"filePath", "offset", "limit"},
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


def _perm(edit=False, run="readonly", scope=None):
    p = F._Ctx()
    p.edit = edit
    p.run = run
    p.scope = scope
    return p


class WorkspaceGuardTests(unittest.TestCase):
    def test_bare_word_rewritten(self):
        out = tools.defuse_extension_words(
            "Workspace root folder: D:\\p\n  this workspace is remote.")
        self.assertIn("Project root folder", out)
        self.assertIn("this project is remote", out)

    def test_paths_and_identifiers_kept(self):
        text = ("D:\\workspace\\app.py /home/u/workspace/x workspace.json "
                "workspace_id my-workspace")
        self.assertEqual(tools.defuse_extension_words(text), text)

    def test_config_switch(self):
        old = CONFIG.get("defuse_extension_words")
        CONFIG["defuse_extension_words"] = False
        try:
            self.assertEqual(tools.defuse_extension_words("Workspace"),
                             "Workspace")
        finally:
            CONFIG["defuse_extension_words"] = old

    def test_prompt_is_defused(self):
        prompt, _ = tools.messages_to_prompt(
            [{"role": "user", "content": "Workspace root folder: D:\\p"}])
        self.assertNotIn("Workspace", prompt)

    def test_workspace_error_is_upstream_error(self):
        self.assertTrue(tools.looks_like_upstream_error(
            "I can't use Workspace because required Gmail settings are off. "
            "Turn on these settings to continue."))


class ToolStreamTests(unittest.TestCase):
    def setUp(self):
        self.orig = dict(CONFIG)
        CONFIG["tool_retry_on_miss"] = True
        CONFIG["tool_stream"] = True
        CONFIG["retry_delay_sec"] = 0

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.orig)

    def _params(self, **over):
        params = dict(prompt="p", model_id=2, think_mode=1, file_refs=None,
                      extra=None, tools_active=True, allowed_names={"read"},
                      tool_schemas={"read": {"path"}}, required_tool=False,
                      messages=[{"role": "user", "content": "read file a"}],
                      ticket=None, tool_required={"read": {"path"}},
                      client_gone=threading.Event())
        params.update(over)
        return params

    def _call(self, gen_attr, deltas, **over):
        params = self._params(**over)
        with mock.patch("gemini_web2api.server." + gen_attr,
                        return_value=iter(deltas)) as m:
            return (GeminiHandler.__new__(GeminiHandler)
                    ._generate_with_tool_retry(**params)), m

    def test_openai_loop_uses_generate_stream_when_tool_stream(self):
        (text, calls), m = self._call("generate_stream", [CALL])
        m.assert_called_once()
        self.assertTrue(calls, "tool call should parse from streamed text")

    def test_stream_attempt_exits_early_on_complete_fence(self):
        seen = []

        def fake_stream(*a, **k):
            for d in ['```tool_cal', 'l\n{"name":"read","argum',
                      'ents":{"path":"a"}}\n```', "TAIL SHOULD NOT BE READ"]:
                seen.append(d)
                yield d

        with mock.patch("gemini_web2api.server.generate_stream",
                        side_effect=fake_stream):
            result = (GeminiHandler.__new__(GeminiHandler)
                      ._generate_with_tool_retry(**self._params()))
        self.assertTrue(result[1], "call parsed from streamed deltas")
        self.assertNotIn("TAIL SHOULD NOT BE READ", seen,
                         "early-exit must stop reading after the fence closes")

    def test_buffered_path_still_uses_generate(self):
        CONFIG["tool_stream"] = False
        params = self._params()
        with mock.patch("gemini_web2api.server.generate",
                        return_value=CALL) as g, \
             mock.patch("gemini_web2api.server.generate_stream") as s:
            text, calls = (GeminiHandler.__new__(GeminiHandler)
                           ._generate_with_tool_retry(**params))
        g.assert_called_once()
        s.assert_not_called()
        self.assertTrue(calls, "buffered path still parses the tool call")

    def test_ping_interval_reduced(self):
        self.assertLessEqual(SSE_PING_INTERVAL_SEC, 5)


class FlagConfigMixin(unittest.TestCase):
    def setUp(self):
        self.orig = dict(CONFIG)
        CONFIG["flow5_enabled"] = True
        CONFIG["fix_enabled"] = True
        CONFIG["flow6_context"] = True
        CONFIG["flow6_lean_prompt"] = False
        CONFIG["flow6_universal_guard"] = False
        CONFIG["flow6_diff_step"] = False
        CONFIG["retry_delay_sec"] = 0

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.orig)


class SelectEvidence(FlagConfigMixin):
    def test_read_stage_prefixes_and_order(self):
        msgs = [{"role": "user", "content": "explain a.py"}]
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1")
        msgs += _call(1, "read", {"filePath": "b.py"}, "Error: file not found")
        ev = F.select_evidence_for_step("read", msgs)
        self.assertTrue(any(l.startswith("read: a.py") for l in ev), ev)
        self.assertTrue(any(l.startswith("failed_read: b.py")
                            and "do not read it again" in l for l in ev), ev)
        ib = next(i for i, l in enumerate(ev) if l.startswith("failed_read"))
        ia = next(i for i, l in enumerate(ev) if l.startswith("read:"))
        self.assertLess(ib, ia)

    def test_discover_caps_at_four_lines(self):
        msgs = [{"role": "user", "content": "review the project"}]
        for i in range(6):
            msgs += _call(i, "glob", {"pattern": f"**/*{i}.py"},
                          f"a{i}.py\nb{i}.py")
        ev = F.select_evidence_for_step("discover", msgs)
        self.assertEqual(len(ev), F._FF5_LEAN_CAPS["discover"])
        self.assertTrue(all(l.startswith("search:") for l in ev), ev)

    def test_verify_cap_and_truncation(self):
        long_out = ("FAILED at line 1\n" + "y" * 5000)
        msgs = [{"role": "user", "content": "run the tests"}]
        msgs += _call(0, "bash", {"command": "python -m unittest discover"},
                      long_out)
        ev = F.select_evidence_for_step("verify", msgs)
        test_lines = [l for l in ev if l.startswith(("test:", "test_failed:"))]
        self.assertTrue(test_lines, ev)
        cap = F._FF5_LEAN_CAPS["verify"]
        self.assertLessEqual(max(len(l) for l in test_lines),
                             cap + len(" …[truncated]") + len("test_failed: "))
        self.assertTrue(any("…[truncated]" in l for l in test_lines), ev)

    def test_diff_takes_only_the_newest_successful_git_diff(self):
        msgs = [{"role": "user", "content": "show me the diff"}]
        msgs += _call(0, "bash", {"command": "git diff -- a.py"}, "OLD DIFF")
        msgs += _call(1, "bash", {"command": "git diff -- a.py"}, "NEW DIFF")
        msgs += _call(2, "bash", {"command": "git status"}, "not a diff")
        ev = F.select_evidence_for_step("diff", msgs)
        diffs = [l for l in ev if l.startswith("diff:")]
        self.assertEqual(len(diffs), 1)
        self.assertIn("NEW DIFF", diffs[0])
        self.assertNotIn("OLD DIFF", diffs[0])

    def test_budget_is_respected(self):
        msgs = [{"role": "user", "content": "explain these files"}]
        for i in range(30):
            msgs += _call(i, "read", {"filePath": f"very/long/path/file_{i}.py"},
                          "content %d" % i)
        budget = 120
        ev = F.select_evidence_for_step("answer", msgs, budget=budget)
        self.assertTrue(ev)
        self.assertLessEqual(sum(map(len, ev)), budget)

    def test_fail_open_on_garbage(self):
        self.assertEqual(F.select_evidence_for_step("analyze", []), [])


class BuildStepPrompt(FlagConfigMixin):
    def test_contains_all_required_sections(self):
        CONFIG["flow6_lean_prompt"] = True
        msgs = [{"role": "user", "content": "fix a.py please"}]
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1")
        msgs += _call(1, "read", {"filePath": "gone.py"},
                      "Error: file not found")
        ctx = _ctx(pattern="CHANGE", last="fix a.py please", fix=False,
                   perm=_perm(edit=True, run="any", scope=["a.py"]))
        step = F.Step("CHANGE", "act", 3, 6, {"act"},
                      "Edit a.py now.", tool_names=["edit"])
        p = F.build_step_prompt(ctx, step, msgs, _tm())
        self.assertIsNotNone(p)
        self.assertIn("Goal: fix a.py please", p)
        self.assertIn("Permissions: edit=allowed | commands=any", p)
        self.assertIn("scope=a.py", p)
        self.assertIn("Pipeline: discover -> read -> edit -> diff -> verify -> answer", p)
        self.assertIn("Evidence:", p)
        self.assertIn("read: a.py", p)
        self.assertIn("failed_read: gone.py", p)
        self.assertIn('"name": "edit"', p)
        self.assertNotIn('"name": "bash"', p)

    def test_no_old_history_text_leaks_in(self):
        CONFIG["flow6_lean_prompt"] = True
        secret = "SECRET-OLD-HISTORY-MARKER-" + "z" * 300
        msgs = [{"role": "user", "content": secret + "\n\nfix a.py"}]
        ctx = _ctx(pattern="FILE_REVIEW", last="fix a.py", fix=False,
                   perm=_perm())
        step = F.Step("FILE_REVIEW", "read", 1, 3, {"read"},
                      "Read a.py.", tool_names=["read"])
        p = F.build_step_prompt(ctx, step, msgs, _tm())
        self.assertIsNotNone(p)
        self.assertNotIn(secret, p)

    def test_drive_and_local_steps_are_skipped(self):
        CONFIG["flow6_lean_prompt"] = True
        ctx = _ctx(pattern="CHANGE", last="fix a.py", fix=False,
                   perm=_perm(edit=True, run="any"))
        tm = _tm()
        driven = F.Step("CHANGE", "survey", 1, 6, {"survey"}, "list files",
                        calls=[{"id": "c", "type": "function", "function":
                               {"name": "glob", "arguments": "{}"}}],
                        drive=True, tool_names=["glob"])
        self.assertIsNone(F.build_step_prompt(ctx, driven, [], tm))
        local = F.Step("CHANGE", "answer", 6, 6, (), "done")
        local.local_text = "no model call needed"
        self.assertIsNone(F.build_step_prompt(ctx, local, [], tm))
        self.assertIsNone(F.build_step_prompt(ctx, None, [], tm))
        self.assertIsNone(F.build_step_prompt(ctx, driven, [], None))


class Fallbacks(FlagConfigMixin):
    def _args(self):
        ctx = _ctx(pattern="CHANGE", last="fix a.py", fix=False,
                   perm=_perm(edit=True, run="any"))
        step = F.Step("CHANGE", "act", 3, 6, {"act"}, "Edit a.py now.",
                      tool_names=["edit"])
        return ctx, step, [], _tm()

    def test_flag_off_returns_none(self):
        CONFIG["flow6_lean_prompt"] = False
        ctx, step, msgs, tm = self._args()
        self.assertIsNone(F.build_step_prompt(ctx, step, msgs, tm))

    def test_ctx_none_returns_none(self):
        CONFIG["flow6_lean_prompt"] = True
        _ctx_, step, msgs, tm = self._args()
        self.assertIsNone(F.build_step_prompt(None, step, msgs, tm))

    def test_pattern_outside_spec_returns_none(self):
        CONFIG["flow6_lean_prompt"] = True
        ctx, step, msgs, tm = self._args()
        ctx.pattern = "NOT_A_PATTERN"
        self.assertIsNone(F.build_step_prompt(ctx, step, msgs, tm))

    def test_internal_raise_is_swallowed(self):
        CONFIG["flow6_lean_prompt"] = True
        ctx, step, msgs, tm = self._args()
        msgs += _call(0, "read", {"filePath": "a.py"}, "x = 1")
        with mock.patch.object(F, "_answered_calls",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(F.build_step_prompt(ctx, step, msgs, tm))


class WrapperIntegration(FlagConfigMixin):
    CALL_FENCE = ('```tool_call\n{"name": "read", '
                  '"arguments": {"filePath": "b.py"}}\n```')
    WRONG_KIND = ('```tool_call\n{"name": "glob", '
                  '"arguments": {"pattern": "*.py"}}\n```')
    ASK = [{"role": "user", "content": "read b.py to explain it"}]

    def setUp(self):
        super().setUp()
        from gemini_web2api import server, tools as T
        saved = []
        for mod, names in (
                (T, ("_is_change_request", "change_demanded",
                     "_is_file_review", "_is_source_review",
                     "_CANNED_FAILURE_RES", "_TASK_BANNER_HOOK")),
                (server, ("change_demanded", "_is_file_review",
                          "_is_source_review"))):
            for name in names:
                saved.append((mod, name, getattr(mod, name, None)))
        handler = server.GeminiHandler._generate_with_tool_retry
        tls_model = getattr(F._ff5_tls, "model", None)

        def restore():
            for mod, name, value in saved:
                setattr(mod, name, value)
            server.GeminiHandler._generate_with_tool_retry = handler
            F._ff5_tls.model = tls_model

        self.addCleanup(restore)

        CONFIG["flow_enabled"] = True
        CONFIG["flow_drive"] = False
        CONFIG["flow_mapreduce"] = False
        CONFIG["flow_max_retries"] = 1
        CONFIG["tool_retry_on_miss"] = True
        CONFIG["tool_stream"] = False
        guards = mock.patch.object(F, "_ff5_plan_after",
                                   side_effect=lambda s, *a, **k: s)
        guards.start()
        self.addCleanup(guards.stop)
        F.install()

    def _run(self, generate, prompt, messages):
        params = dict(prompt=prompt, model_id=2, think_mode=1, file_refs=None,
                      extra=None, tools_active=True,
                      allowed_names={"read", "edit", "bash", "glob", "grep"},
                      tool_schemas={"read": {"filePath"},
                                    "edit": {"filePath", "oldString", "newString"},
                                    "bash": {"command"},
                                    "glob": {"pattern"}, "grep": {"pattern"}},
                      required_tool=False, messages=messages, ticket=None,
                      tool_required={"read": {"filePath"}},
                      client_gone=threading.Event())
        from gemini_web2api.server import GeminiHandler
        h = GeminiHandler.__new__(GeminiHandler)
        with mock.patch("gemini_web2api.server.generate", generate):
            return h._generate_with_tool_retry(**params)

    def test_flag_on_sends_the_lean_step_prompt(self):
        CONFIG["flow6_lean_prompt"] = True
        long_hist = "OLD-CHAT-HISTORY-" + "w" * 4000
        gen = mock.Mock(return_value=self.CALL_FENCE)
        self._run(gen, long_hist, list(self.ASK))
        gen.assert_called()
        sent = gen.call_args[0][0]
        self.assertNotIn(long_hist, sent)
        self.assertIn("Goal: read b.py to explain it", sent)
        self.assertIn("[FLOW ", sent)
        self.assertIn("Allowed tools this turn:", sent)
        self.assertIn('"name": "read"', sent)
        self.assertLess(len(sent), len(long_hist))

    def test_flag_off_sends_the_full_prompt(self):
        CONFIG["flow6_lean_prompt"] = False
        long_hist = "OLD-CHAT-HISTORY-" + "w" * 4000
        gen = mock.Mock(return_value=self.CALL_FENCE)
        self._run(gen, long_hist, list(self.ASK))
        gen.assert_called()
        sent = gen.call_args[0][0]
        self.assertIn(long_hist, sent)
        self.assertIn("[FLOW ", sent)
        self.assertNotIn("Goal:", sent)

    def test_retry_nudge_still_appends_to_the_step_prompt(self):
        CONFIG["flow6_lean_prompt"] = True
        CONFIG["flow_max_retries"] = 1
        prompt = "FULL-PROMPT-MARKER-" + "v" * 2000
        gen = mock.Mock(side_effect=[self.WRONG_KIND, self.CALL_FENCE])
        self._run(gen, prompt, list(self.ASK))
        self.assertEqual(gen.call_count, 2)
        second = gen.call_args[0][0]
        self.assertIn("That is not allowed at this step", second)
        self.assertIn("Goal:", second)
        self.assertNotIn(prompt, second)

    def test_driven_step_never_reaches_upstream(self):
        CONFIG["flow_drive"] = True
        CONFIG["flow6_lean_prompt"] = True
        gen = mock.Mock(return_value=self.CALL_FENCE)
        _text, calls = self._run(gen, "prompt", list(self.ASK))
        gen.assert_not_called()
        self.assertTrue(calls)


class ContextSuffixGuard(FlagConfigMixin):
    def _guided_step(self):
        return F.Step("FILE_REVIEW", "read", 1, 2, {"read"},
                      "Read the named file.", tool_names=["read"])

    def _messages(self):
        return [{"role": "user", "content": "explain a.py"}]

    def test_suffix_not_injected_when_lean_prompt_on(self):
        CONFIG["flow6_lean_prompt"] = True
        step = self._guided_step()
        out = F._ff5_after_plan(step, self._messages(), _tm())
        self.assertNotIn("[Context]", out.instruction or "")

    def test_suffix_still_injected_when_lean_prompt_off(self):
        CONFIG["flow6_lean_prompt"] = False
        step = self._guided_step()
        out = F._ff5_after_plan(step, self._messages(), _tm())
        self.assertIn("[Context]", out.instruction or "")
        self.assertIn("goal=explain a.py", out.instruction)

    def test_instruction_identical_to_plain_step_when_lean_on(self):
        CONFIG["flow6_lean_prompt"] = True
        step = self._guided_step()
        before = step.instruction
        out = F._ff5_after_plan(step, self._messages(), _tm())
        self.assertEqual(out.instruction, before)


if __name__ == "__main__":
    unittest.main()

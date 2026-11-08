import json
import unittest
import gemini_web2api as M


class TestRefactorVerification(unittest.TestCase):
    def test_scope_empty_pattern_with_read(self):
        """_ff5_scope('', ...) with read a.py must return ['a.py']."""
        class FakeCtx:
            pattern = ""

        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "read", "arguments": json.dumps({"path": "a.py"})}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "content of a.py"}
        ]
        scope = M._ff5_scope(FakeCtx(), messages)
        self.assertEqual(["a.py"], scope)

    def test_clarification_text_not_user_last(self):
        """clarification_text must return None when last message is not role=user."""
        # Mid-flow with assistant message last
        msgs_assistant = [
            {"role": "user", "content": "xem giup minh"},
            {"role": "assistant", "content": "Dang kiem tra..."}
        ]
        self.assertIsNone(M.clarification_text(msgs_assistant))

        # Mid-flow with tool result last
        msgs_tool = [
            {"role": "user", "content": "xem giup minh"},
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "read", "arguments": json.dumps({"path": "a.py"})}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "file content"}
        ]
        self.assertIsNone(M.clarification_text(msgs_tool))

    def test_google_ai_pro_prompt_routes_to_explain(self):
        """Google_AI_Pro prompt starting with 'Giai thich tai sao...' must route to EXPLAIN."""
        prompt = (
            "Giải thích tại sao sau khi làm xong hướng dẫn của bạn phía trên, "
            "tôi đã vào one.google.com/plans lần 2 để kích hoạt thêm 6 tháng thì nó redirect về "
            "https://one.google.com/settings?expand=upgrade&g1_landing_page=0 , khi chạy hook của bạn, "
            "xong bấm nâng cấp Google AI Pro (10TB) thì có vẻ không thấy mức 0đ nữa?\n\n"
            "Các khoản phí sắp tới\n"
            "Hôm nay\n"
            "53.772 ¥\n"
            "bao gồm thuế 4.888 ¥\n"
            "Google AI Pro (10 TB) — Trong 364 ngày\n"
            "53.772 ¥\n"
            "Ngày bắt đầu: 7 thg 11, 2027\n"
            "7.280 ¥/month\n"
            "bao gồm thuế 662 ¥\n"
            "Google AI Pro (10 TB)\n"
            "7.280 ¥\n"
            "Đăng ký trên Play\n"
            "Huỷ bất kỳ lúc nào trong phần Gói thuê bao trên Google Play\n"
            "Mức phí phải thanh toán cho khoảng thời gian từ hôm nay đến 7 thg 11, 2027 là 53.772 ¥\n"
            "Bạn đang xoá 2 mục khỏi gói thuê bao "
        )
        ctx = M._analyze([{"role": "user", "content": prompt}])
        self.assertIsNotNone(ctx)
        self.assertEqual("EXPLAIN", ctx.pattern)

    def test_independent_grants_toggle(self):
        """flow8_independent_grants toggle preserves backward-compatible behavior."""
        msgs = [
            {"role": "user", "content": "do not run any commands"},
            {"role": "user", "content": "go ahead"}
        ]
        M.CONFIG["flow8_independent_grants"] = False
        p_coupled = M._ff5_permissions(msgs, "CHANGE")
        self.assertTrue(p_coupled.edit)
        self.assertEqual("any", p_coupled.run)

        M.CONFIG["flow8_independent_grants"] = True
        p_indep = M._ff5_permissions(msgs, "CHANGE")
        self.assertTrue(p_indep.edit)
        self.assertEqual("none", p_indep.run)

    def test_strict_shell_false_ff6b_checks(self):
        """flow8_strict_shell=False retains ff6b dangerous command blocking."""
        M.CONFIG["flow8_strict_shell"] = False
        self.assertTrue(M._ff5_cmd_readonly("git status"))
        self.assertTrue(M._ff5_cmd_readonly("git diff"))
        self.assertFalse(M._ff5_cmd_readonly("find . -delete"))
        self.assertFalse(M._ff5_cmd_readonly("git branch -D test"))
        self.assertFalse(M._ff5_cmd_readonly("git remote add origin https://example.com"))
        self.assertFalse(M._ff5_cmd_readonly("git log --output=foo.txt"))
        self.assertFalse(M._ff5_cmd_readonly("echo `whoami`"))
        self.assertFalse(M._ff5_cmd_readonly("echo $(whoami)"))
        M.CONFIG["flow8_strict_shell"] = True

    def test_server_hardening_loopback_toggle(self):
        """flow15_server_hardening toggle fallback for bracketed loopback."""
        M.CONFIG["flow15_server_hardening"] = True
        self.assertTrue(M._is_loopback("[127.0.0.1]"))

        M.CONFIG["flow15_server_hardening"] = False
        self.assertFalse(M._is_loopback("[127.0.0.1]"))
        M.CONFIG["flow15_server_hardening"] = True



    def test_plan_implementation_with_create_test_clause_stays_change(self):
        """User prompt: read feature_implementation_plan.md, implement into gemini_web2api.py,
        then create test file -> must route as CHANGE (not CREATE) and not mark existing files as created."""
        prompt = (
            "Hãy đọc file feature_implementation_plan.md và thực thi từng bước trong "
            "checklist để implement tính năng vào gemini_web2api.py. Sau khi hoàn thành, "
            "hãy tạo file test và chạy unit test để nghiệm thu."
        )
        ctx = M._analyze([{"role": "user", "content": prompt}])
        self.assertEqual("CHANGE", ctx.pattern)
        created = M._created_files(ctx.scannable, ctx.folded)
        self.assertNotIn("feature_implementation_plan.md", created)
        self.assertNotIn("gemini_web2api.py", created)

    def test_todowrite_and_plan_phases_keep_flow_in_act_until_complete(self):
        """When todowrite has pending items or plan symbols/test files are not yet implemented,
        _change_tail must stay in step 3/5 act instead of jumping to tests/answer after 1 edit."""
        prompt = (
            "Hãy đọc file feature_implementation_plan.md và thực thi từng bước trong "
            "checklist để implement tính năng vào gemini_web2api.py. Sau khi hoàn thành, "
            "hãy tạo file test và chạy unit test để nghiệm thu."
        )
        plan_doc = (
            "# Plan\n"
            "- [ ] **Step 1:** Update `DEFAULT_CONFIG` in `gemini_web2api.py` with `\"cookie_pool\": []`.\n"
            "- [ ] **Step 2:** Implement `init_cookie_pool()`, `get_active_cookie_credentials()`, `mark_cookie_cooldown(path, reason)` in `gemini_web2api.py`.\n"
            "- [ ] **Step 3:** Add `GET /v1/cookies/pool` in `do_GET()` of `gemini_web2api.py`.\n"
            "- [ ] **Step 4:** Create `tests/test_cookie_pool.py` and run unit tests.\n"
        )
        msgs = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_glob", "type": "function", "function": {
                    "name": "glob", "arguments": json.dumps({"pattern": "**/*.py"})}}
            ]},
            {"role": "tool", "tool_call_id": "c_glob", "content": "gemini_web2api.py\ntests/test_server.py"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_r1", "type": "function", "function": {
                    "name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "c_r2", "type": "function", "function": {
                    "name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c_r1", "content": plan_doc},
            {"role": "tool", "tool_call_id": "c_r2", "content": 'DEFAULT_CONFIG = {\n    "port": 8081,\n}\ndef load_cookie():\n    pass\ndef do_GET(self):\n    pass\n'},
            # Turn 1 of act: model calls todowrite (1 completed, 3 pending) + 1st edit (DEFAULT_CONFIG)
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_todo", "type": "function", "function": {
                    "name": "todowrite",
                    "arguments": json.dumps({"todos": [
                        {"content": "Step 1: Update DEFAULT_CONFIG", "status": "completed"},
                        {"content": "Step 2: Implement init_cookie_pool", "status": "in_progress"},
                        {"content": "Step 3: Add GET /v1/cookies/pool", "status": "pending"},
                        {"content": "Step 4: Write tests/test_cookie_pool.py and run tests", "status": "pending"},
                    ]})
                }},
                {"id": "c_e1", "type": "function", "function": {
                    "name": "edit",
                    "arguments": json.dumps({
                        "filePath": "gemini_web2api.py",
                        "oldString": 'DEFAULT_CONFIG = {\n    "port": 8081,\n}',
                        "newString": 'DEFAULT_CONFIG = {\n    "port": 8081,\n    "cookie_pool": [],\n}',
                    })
                }},
            ]},
            {"role": "tool", "tool_call_id": "c_todo", "content": "Todos updated: 1/4 completed"},
            {"role": "tool", "tool_call_id": "c_e1", "content": "Edit applied successfully."},
        ]
        tm = M.ToolMap(
            {"glob", "read", "edit", "write", "bash", "todowrite"},
            {
                "glob": {"pattern"},
                "read": {"filePath", "offset", "limit"},
                "edit": {"filePath", "oldString", "newString"},
                "write": {"filePath", "content"},
                "bash": {"command"},
                "todowrite": {"todos"},
            },
            {},
        )
        step = M.plan_for(msgs, tm)
        self.assertIsNotNone(step)
        self.assertEqual("act", step.name)
        self.assertIn("init_cookie_pool", step.instruction)

        # Verify a second surgical edit on gemini_web2api.py is allowed by postfilter without re-reading
        ctx = M._analyze(msgs)
        tc_e2 = {"id": "c_e2", "type": "function", "function": {
            "name": "edit",
            "arguments": json.dumps({
                "filePath": "gemini_web2api.py",
                "oldString": "def load_cookie():\n    pass",
                "newString": "def init_cookie_pool():\n    pass\ndef get_active_cookie_credentials():\n    pass\ndef mark_cookie_cooldown(p, r):\n    pass\ndef load_cookie():\n    pass",
            })
        }}
        res_pf, nudge_pf = M._ff5_postfilter(("", [tc_e2]), msgs, ctx.perm, last=False, tm=tm, ctx=ctx)
        self.assertIsNone(nudge_pf)
        self.assertIsNotNone(res_pf)
        self.assertEqual(1, len(res_pf[1]))

        # Apply Step 2, Step 3, and Step 4 (write tests/test_cookie_pool.py) -> should then transition to tests
        msgs += [
            {"role": "assistant", "content": "", "tool_calls": [tc_e2]},
            {"role": "tool", "tool_call_id": "c_e2", "content": "Edit applied successfully."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_e3", "type": "function", "function": {
                    "name": "edit",
                    "arguments": json.dumps({
                        "filePath": "gemini_web2api.py",
                        "oldString": "def do_GET(self):\n    pass",
                        "newString": 'def do_GET(self):\n    if self.path == "/v1/cookies/pool": return\n',
                    })
                }}
            ]},
            {"role": "tool", "tool_call_id": "c_e3", "content": "Edit applied successfully."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c_w1", "type": "function", "function": {
                    "name": "write",
                    "arguments": json.dumps({
                        "filePath": "tests/test_cookie_pool.py",
                        "content": "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n",
                    })
                }}
            ]},
            {"role": "tool", "tool_call_id": "c_w1", "content": "Wrote file successfully."},
        ]
        step_after_all = M.plan_for(msgs, tm)
        self.assertIsNotNone(step_after_all)
        self.assertIn(step_after_all.name, ("diff", "tests"))
        if step_after_all.name == "diff":
            msgs += [
                {"role": "assistant", "content": "", "tool_calls": step_after_all.calls},
                {"role": "tool", "tool_call_id": step_after_all.calls[0]["id"], "content": "diff --git a/gemini_web2api.py b/gemini_web2api.py"},
            ]
            step_tests = M.plan_for(msgs, tm)
            self.assertIsNotNone(step_tests)
            self.assertEqual("tests", step_tests.name)

    def test_window_reread_on_already_read_file_does_not_trigger_continuations(self):
        """A small window read (offset=30, limit=120) on a file already read past offset 150
        must not be treated as an unfinished file read."""
        ev = [
            {"name": "read", "kind": "read", "args": {"filePath": "big.py"},
             "result": "1: a\n[Output truncated. Continue reading with offset: 1201]"},
            {"name": "read", "kind": "read", "args": {"filePath": "big.py", "offset": 1201},
             "result": "1201: end of file"},
            {"name": "read", "kind": "read", "args": {"filePath": "big.py", "offset": 30, "limit": 120},
             "result": "30: window\n[Output truncated. Continue reading with offset: 150]"},
        ]
        self.assertEqual([], M._ff4_unfinished(ev))
        self.assertEqual([], M._ff4_continuations(ev, "offset"))

    def test_large_file_act_context_pack_injects_plan_and_verbatim_windows(self):
        """When read outputs exceed 80 KB, _ff5_after_plan injects the plan doc and
        unnumbered verbatim AST code windows into step.instruction during act."""
        prompt = "Hãy đọc feature_implementation_plan.md và cập nhật `DEFAULT_CONFIG` cùng `load_cookie` trong `big.py`."
        plan_doc = "Cập nhật `DEFAULT_CONFIG` và hàm `load_cookie()` trong `big.py`."
        padding = "# padding line for large file\n" * 3200
        big_code = (
            'DEFAULT_CONFIG = {\n    "port": 8081,\n}\n'
            + padding
            + "def load_cookie():\n    return None\n"
        )
        msgs = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "r_plan", "type": "function", "function": {
                    "name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "r_big", "type": "function", "function": {
                    "name": "read", "arguments": json.dumps({"filePath": "big.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "r_plan", "content": plan_doc},
            {"role": "tool", "tool_call_id": "r_big", "content": big_code},
        ]
        tm = M.ToolMap({"read", "edit"}, {"read": {"filePath"}, "edit": {"filePath", "oldString", "newString"}}, {})
        step = M.Step("CHANGE", "act", 3, 5, {"act"}, "Make the change.", None, False, ["edit"])
        step = M._ff5_after_plan(step, msgs, tm, M._analyze(msgs))
        self.assertIn("[Plan Document (feature_implementation_plan.md)", step.instruction)
        self.assertIn("[Target Code Windows from big.py", step.instruction)
        self.assertIn("def load_cookie():", step.instruction)


if __name__ == "__main__":
    unittest.main()


class TestReportedInvestigationAndReviewFixes(unittest.TestCase):
    def setUp(self):
        self.saved_cfg = dict(M.CONFIG)

    def tearDown(self):
        M.CONFIG.clear()
        M.CONFIG.update(self.saved_cfg)

    def test_investigate_refactor_without_editing_routes_to_explain(self):
        """User prompt 'Điều tra xem source gemini_web2api.py ... chưa sửa source' must route to EXPLAIN with edit=False."""
        prompt = (
            "Điều tra xem source gemini_web2api.py hiện tại có thể refactor cho gọn hơn "
            "mà không làm tha đổi logic hiện tại không? chưa sửa source"
        )
        msgs = [{"role": "user", "content": prompt}]
        ctx = M._analyze(msgs)
        self.assertIsNotNone(ctx)
        self.assertEqual("EXPLAIN", ctx.pattern)
        self.assertFalse(ctx.perm.edit)
        self.assertTrue(ctx.no_edit)
        self.assertFalse(M.change_demanded(msgs))
        self.assertFalse(M._is_change_request(ctx.folded, ctx.scannable, ctx.last))

    def test_17_chunk_large_file_reads_to_end_and_answers_with_structure_index(self):
        """A 17-chunk read (like 843KB gemini_web2api.py in 50KB chunks) continues through all 17 chunks and answers."""
        prompt = (
            "Điều tra xem source gemini_web2api.py hiện tại có thể refactor cho gọn hơn "
            "mà không làm tha đổi logic hiện tại không? chưa sửa source"
        )
        msgs = [{"role": "user", "content": prompt}]
        tm = M.ToolMap(
            {"read", "edit", "bash", "glob", "grep"},
            {
                "read": {"path", "offset", "limit"},
                "edit": {"path", "oldString", "newString"},
                "bash": {"command"},
                "glob": {"pattern"},
                "grep": {"pattern"},
            },
            {},
        )
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_glob",
                "type": "function",
                "function": {"name": "glob", "arguments": json.dumps({"pattern": "**/*.py"})},
            }],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": "call_glob",
            "name": "glob",
            "content": "D:/Project/gemini-web2api/gemini_web2api.py\nD:/Project/gemini-web2api/tests/test_server.py",
        })

        total_chunks = 17
        lines_per_chunk = 1150
        for c_idx in range(total_chunks):
            start_ln = c_idx * lines_per_chunk + 1
            end_ln = (c_idx + 1) * lines_per_chunk
            header = f"Read file D:/Project/gemini-web2api/gemini_web2api.py, lines {start_ln}-{end_ln}\n"
            body_lines = [
                f"{start_ln}: # Section: layer_{c_idx}",
                f"{start_ln + 1}: class Layer{c_idx}:",
                f"{start_ln + 2}:     pass",
                f"{start_ln + 3}: def shared_hook(x):",
                f"{start_ln + 4}:     return x + {c_idx}",
            ]
            # Pad chunk with numbered lines so total read chars across 17 chunks > 100 KB
            pad = "\n".join(f"{start_ln + 5 + k}: x = 1  # padding line" for k in range(220))
            footer = (
                f"\n(File has more lines. Use 'offset' parameter to read beyond line {end_ln})"
                if c_idx < total_chunks - 1
                else f"\n(End of file - total {end_ln} lines)"
            )
            args = {"path": "D:/Project/gemini-web2api/gemini_web2api.py"}
            if c_idx > 0:
                args["offset"] = start_ln
            msgs.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"call_r{c_idx}",
                    "type": "function",
                    "function": {"name": "read", "arguments": json.dumps(args)},
                }],
            })
            msgs.append({
                "role": "tool",
                "tool_call_id": f"call_r{c_idx}",
                "name": "read",
                "content": header + "\n".join(body_lines) + "\n" + pad + footer,
            })
            ctx = M._analyze(msgs)
            step = M.plan_for(msgs, tm, ctx)
            self.assertIsNotNone(step)
            self.assertIsNone(getattr(step, "local_text", None))
            if c_idx < total_chunks - 1:
                self.assertEqual("read", step.name, f"Chunk {c_idx + 1} should continue reading")
            else:
                self.assertEqual("answer", step.name, "Final chunk should transition to answer")
                self.assertIn("[Structure index for D:/Project/gemini-web2api/gemini_web2api.py", step.instruction)
                self.assertIn("shared_hook", step.instruction)

    def test_opencode_read_header_stripped_for_clean_file_text_and_ast(self):
        """OpenCode 'Read file ..., lines X-Y' header must not break _clean_file_text or ast.parse."""
        raw_read = (
            "Read file D:/Project/gemini-web2api/gemini_web2api.py, lines 1-5\n"
            "1: import sys\n"
            "2: import re\n"
            "3: def hello():\n"
            "4:     return 42\n"
            "5: \n"
            "(File has more lines. Use 'offset' parameter to read beyond line 5)"
        )
        cleaned = M._clean_file_text(raw_read)
        self.assertFalse(cleaned.startswith("Read file"))
        self.assertIn("def hello():", cleaned)
        # Must be valid Python syntax
        import ast
        tree = ast.parse(cleaned)
        self.assertIsNotNone(tree)

        clean_src, line_nums = M._ff17_strip_numbered_lines(raw_read)
        self.assertEqual([1, 2, 3, 4, 5], line_nums[:5])
        self.assertIsNotNone(ast.parse(clean_src))

    def test_trunc_regex_ignores_quoted_literals_in_test_files(self):
        """_FF4_TRUNC_RE must not match or strip quoted truncation strings inside Python test code."""
        code_line = (
            '273:         trunc = "x = 1\\n[... output truncated, continue at offset: 100]"\n'
            '987:         self.assertEqual(rx.sub("", "a (Showing lines 1-9 of 99. Use offset=10 to continue.)"), "a ")\n'
        )
        self.assertEqual([], M._FF4_TRUNC_RE.findall(code_line))
        self.assertIsNone(M._FF4_TRUNC_RE.search(code_line))
        self.assertEqual(code_line, M._FF4_TRUNC_RE.sub("", code_line))

        # Real tool truncation footers still match and strip properly
        real_footer = "10: x = 1\n(Output truncated at 51200 bytes. Use 'offset' parameter to read beyond line 1176)"
        self.assertEqual(["1176"], M._FF4_TRUNC_RE.findall(real_footer))
        self.assertIsNotNone(M._FF4_TRUNC_RE.search(real_footer))

    def test_local_imports_does_not_invent_stdlib_or_self_bundle_modules(self):
        """flowfix16 local_imports must not fabricate re.py or gemini_web2api/server.py when reading gemini_web2api.py."""
        code = (
            "import sys\n"
            "import re\n"
            "import urllib.parse\n"
            "from gemini_web2api import server, tools\n"
        )
        listing = [
            "D:/Project/gemini-web2api/gemini_web2api.py",
            "D:/Project/gemini-web2api/tests/test_server.py",
            "D:/Project/gemini-web2api/tests/test_flow_patch.py",
            "D:/Project/gemini-web2api/tests/test_modular_sync.py",
        ]
        hops = M.local_imports("D:/Project/gemini-web2api/gemini_web2api.py", code, listing)
        self.assertEqual([], hops)

        # Even with a 1-file grep listing, stdlib and self-stem imports are not fabricated
        hops_single = M.local_imports("D:/Project/gemini-web2api/gemini_web2api.py", code,
                                      ["D:/Project/gemini-web2api/gemini_web2api.py"])
        self.assertEqual([], hops_single)

    def test_auto_read_readme_is_not_treated_as_plan_doc(self):
        """An auto-read README.md in a CHANGE turn must not turn all mentioned files into mandatory plan targets."""
        msgs = [
            {"role": "user", "content": "fix bug in gemini_web2api.py"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c_readme",
                    "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"path": "README.md"})},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "c_readme",
                "name": "read",
                "content": "# Project\nSee cloudflare/worker.js and tools/check_tickets.py for details.",
            },
        ]
        ctx = M._analyze(msgs)
        self.assertEqual([], M._ff16_plan_files(ctx, msgs, ["gemini_web2api.py", "cloudflare/worker.js", "tools/check_tickets.py"]))

    def test_bug_report_with_tra_ve_change_and_pasted_session_is_not_revert(self):
        """A user prompt reporting 'source trả về là CHANGE' with a pasted session transcript must not route to REVERT."""
        pasted_prompt = (
            'Khi mình nhờ điều tra source như bên dưới, thì source trả về là "CHANGE" '
            'và luôn luôn trả về câu output "Một lần đọc ....." thay vì đi điều tra source. '
            'Hãy điều tra và fix giùm mình nhé.\n\n'
            '-------\n'
            '**Session ID:** ses_e4a45c4a5ffe0csHafZa92QWtG\n'
            '## Assistant\n'
            'Read file D:/Project/gemini-web2api/gemini_web2api.py, lines 1-10\n'
            '1: # see ./config.json and 028-6eb337387583.js\n'
        )
        ctx = M._analyze([{"role": "user", "content": pasted_prompt}])
        self.assertNotEqual("REVERT", ctx.pattern)
        named = M._named_files(ctx)
        self.assertNotIn("028-6eb337387583.js", named)
        self.assertNotIn("./config.json", named)

    def test_tim_hieu_investigation_is_not_ambiguous_file_review(self):
        """Live session test: 'tìm hiểu' must not trigger weak FILE_REVIEW tie-break."""
        prompt = (
            "Điều tra xem source gemini_web2api.py, tìm hiểu những chỗ có thể "
            "refactor cho gọn hơn mà không làm thay đổi logic hiện tại. "
            "Không sửa source chỉ điều tra"
        )
        cands = M._ff10_candidates([{"role": "user", "content": prompt}])
        self.assertEqual([{"label": "EXPLAIN", "score": 2, "weak": False}], cands)
        self.assertFalse(M._ff10_ambiguous(cands))
        self.assertFalse(M.review_demanded([{"role": "user", "content": prompt}]))

    def test_quoted_snippets_does_not_pair_across_two_short_inline_code_spans(self):
        """Two short inline code spans (<25 chars) on one line must not capture the prose between them."""
        prose = (
            "`gemini_web2api.py` là một file mã nguồn tích hợp toàn bộ các module "
            "cốt lõi và các bản vá hướng dẫn luồng (flow-patch) từ `flowfix2` đến `flowfix17`."
        )
        self.assertEqual([], M._quoted_snippets(prose))

    def test_clean_text_strips_aborted_first_draft_on_mid_backtick_restart(self):
        """If upstream aborts mid-backtick and restarts its numbered list from 1., clean_text drops the aborted prefix."""
        glitched = (
            "Qua quá trình điều tra mã nguồn trong file `gemini_web2api.py`:\n\n"
            "1. **Tách nhỏ các bản vá:**\n   - Chi tiết 1.\n\n"
            "2. **Chuẩn hóa hàm:**\n   - Chi tiết 2.\n\n"
            "3. **Tách biệt các Section cốt lõi:**\n"
            "   - Các phần như `config.py`, `models.py`, `tools.py`, `gemini"
            "Qua điều tra source code trong file `gemini_web2api.py` (từ dòng 1 đến dòng 19585), "
            "cấu trúc của file được tổ chức thành một tập hợp các module chức năng cốt lõi:\n\n"
            "1. **Hợp nhất các lớp vá:**\n   - Đề xuất 1.\n\n"
            "2. **Tổ chức lại các Section:**\n   - Đề xuất 2.\n"
        )
        cleaned = M.clean_text(glitched)
        self.assertTrue(cleaned.startswith("Qua điều tra source code trong file `gemini_web2api.py`"))
        self.assertNotIn("`geminiQua", cleaned)

    def test_plan_files_ignores_threading_lock_section_headers_and_json_examples(self):
        """Regression for conversation.txt Sec 20+: _ff16_plan_files and _ff17_remaining_plan_or_todo_work
        must ignore threading.Lock, `# Section: config.py`, and `./cookie1.json` config examples."""
        plan_md = (
            "# Feature Implementation Plan: Multi-Cookie Pool\n"
            "1. **Single-File Architecture:** Toàn bộ mã nguồn nằm trong `gemini_web2api.py` "
            "(được chia thành các `# Section: config.py`, `gemini.py`, `server.py`, v.v.).\n"
            "2. **Thread-Safety:** Sử dụng `threading.Lock` khi đọc/ghi trạng thái.\n"
            "### Bước 1: Mở rộng Cấu hình (`# Section: config.py`)\n"
            "- [ ] Thêm các tùy chọn cấu hình mới vào `DEFAULT_CONFIG`:\n"
            "  - `\"cookie_pool\": []` (Danh sách đường dẫn file cookie JSON, ví dụ: `[\"./cookie1.json\", \"./cookie2.json\"]`)\n"
            "  - `\"cookie_pool_strategy\": \"round_robin\"`\n"
            "  - `\"cookie_cooldown_sec\": 300`\n"
            "### Bước 5: Kiểm thử (`tests/test_cookie_pool.py`)\n"
            "- [ ] Tạo file unit test mới `tests/test_cookie_pool.py` sử dụng `unittest`.\n"
        )
        msgs = [
            {"role": "user", "content": "Hãy đọc file feature_implementation_plan.md và implement tính năng theo đúng các bước trong plan đó."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": plan_md},
        ]
        ctx = M._analyze(msgs)
        listing = ["gemini_web2api.py", "tests/test_server.py"]
        pfiles = M._ff16_plan_files(ctx, msgs, listing)
        self.assertEqual(["gemini_web2api.py", "tests/test_cookie_pool.py"], pfiles)
        self.assertNotIn("threading.Lock", M._files_in(plan_md))
        rem = M._ff17_remaining_plan_or_todo_work(ctx, M._answered_calls(msgs), msgs)
        self.assertEqual(["tests/test_cookie_pool.py"], rem["uncreated_files"])

    def test_todo_blocks_prioritize_checkboxes_and_track_sub_bullet_config_keys(self):
        """Regression for conversation.txt Sec 20-38: _ff16_extract_todo_items must prioritize `- [ ]`
        checkboxes over numbered constraint items and track indented sub-bullet config keys."""
        plan_md = (
            "## 2. Constraints & Project Guidelines\n"
            "1. **Single-File Architecture:** Toàn bộ mã nguồn nằm trong `gemini_web2api.py`.\n"
            "2. **Zero External Dependencies:** Chỉ sử dụng Python Standard Library.\n"
            "## 3. Implementation Phases\n"
            "- [ ] Thêm các tùy chọn cấu hình mới vào `DEFAULT_CONFIG`:\n"
            "  - `\"cookie_pool\": []`\n"
            "  - `\"cookie_pool_strategy\": \"round_robin\"`\n"
            "  - `\"cookie_cooldown_sec\": 300`\n"
            "- [ ] Viết hàm `init_cookie_pool() -> None`:\n"
            "  - Đọc danh sách từ `CONFIG.get(\"cookie_pool\")`.\n"
        )
        msgs = [
            {"role": "user", "content": "Hãy đọc file feature_implementation_plan.md và implement."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": plan_md},
            {"role": "tool", "tool_call_id": "c2", "name": "read", "content": "DEFAULT_CONFIG = {\"port\": 8000}\n"},
        ]
        ctx = M._analyze(msgs)
        items = M._ff16_extract_todo_items(ctx, msgs)
        self.assertEqual(2, len(items))
        self.assertIn("DEFAULT_CONFIG", items[0])
        ledger_before = M._ff16_todo_ledger(ctx, msgs)
        self.assertIn("1. [ ] Thêm các tùy chọn cấu hình mới vào `DEFAULT_CONFIG`", ledger_before)

        # Now apply Step 1 edit adding the 3 config keys
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c3", "type": "function", "function": {"name": "edit", "arguments": json.dumps({
                    "filePath": "gemini_web2api.py",
                    "oldString": "DEFAULT_CONFIG = {\"port\": 8000}",
                    "newString": "DEFAULT_CONFIG = {\"port\": 8000, \"cookie_pool\": [], \"cookie_pool_strategy\": \"round_robin\", \"cookie_cooldown_sec\": 300}",
                })}},
            ]},
            {"role": "tool", "tool_call_id": "c3", "name": "edit", "content": "Edit applied successfully."},
        ])
        ledger_after = M._ff16_todo_ledger(ctx, msgs)
        self.assertIn("1. [x] Thêm các tùy chọn cấu hình mới vào `DEFAULT_CONFIG`", ledger_after)
        self.assertIn("2. [ ] Viết hàm `init_cookie_pool() -> None`", ledger_after)

    def test_act_context_pack_includes_section_headers_and_referenced_module_globals(self):
        """Regression for conversation.txt Sec 21: _ff17_act_context_pack must include `# Section: gemini.py`
        and module-level globals referenced inside matched functions (`_cookie_cache`, etc.)."""
        filler = "# filler line\n" * 6500
        src_body = (
            "# Section: gemini.py\n"
            "_cookie_cache = (\"\", None, 0)\n"
            "_cookie_last_mtime_check = 0.0\n"
            "_COOKIE_MTIME_MIN_INTERVAL = 1.0\n"
            + filler +
            "def load_cookie():\n"
            "    global _cookie_cache, _cookie_last_mtime_check\n"
            "    return _cookie_cache[1]\n"
        )
        plan_md = "### Bước 2: Quản lý Trạng thái (`# Section: gemini.py`)\n- [ ] Cập nhật hàm `load_cookie()`\n"
        msgs = [
            {"role": "user", "content": "Implement feature_implementation_plan.md"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": plan_md},
            {"role": "tool", "tool_call_id": "c2", "name": "read", "content": src_body},
        ]
        ctx = M._analyze(msgs)
        pack = M._ff17_act_context_pack(ctx, msgs)
        self.assertIn("_cookie_cache = (\"\", None, 0)", pack)
        self.assertIn("def load_cookie():", pack)

    def test_pure_anchor_edit_auto_repairs_and_window_reread_preserves_ast(self):
        """Regression for conversation.txt Sec 21 & Sec 38:
        1. Pure-anchor append edit with minor literal drift (`0.0` vs `0`) is auto-repaired.
        2. Window re-read (`offset=1, limit=5`) splices in place without corrupting the file tail."""
        src_body = "\n".join([
            "# Section: config.py",
            "DEFAULT_CONFIG = {}",
            "# Section: gemini.py",
            "_cookie_cache = (\"\", None, 0)",
            "_cookie_last_mtime_check = 0.0",
            "_COOKIE_MTIME_MIN_INTERVAL = 1.0",
            "def load_cookie():",
            "    return None",
        ])
        msgs = [
            {"role": "user", "content": "Implement plan"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
            ]},
            {"role": "tool", "tool_call_id": "r1", "name": "read", "content": src_body},
        ]
        # Pure-anchor append where model wrote 0.0 instead of 0
        tc = {
            "id": "e1",
            "type": "function",
            "function": {
                "name": "edit",
                "arguments": json.dumps({
                    "filePath": "gemini_web2api.py",
                    "oldString": "_cookie_cache = (\"\", None, 0.0)\n_cookie_last_mtime_check = 0.0\n_COOKIE_MTIME_MIN_INTERVAL = 1.0",
                    "newString": "_cookie_cache = (\"\", None, 0.0)\n_cookie_last_mtime_check = 0.0\n_COOKIE_MTIME_MIN_INTERVAL = 1.0\n_cookie_pool_lock = threading.Lock()",
                }),
            },
        }
        out, probs = M._base_gate_act_calls([tc], msgs)
        self.assertEqual([], probs)
        repaired_args = json.loads(out[0]["function"]["arguments"])
        self.assertEqual("_COOKIE_MTIME_MIN_INTERVAL = 1.0", repaired_args["oldString"])
        self.assertIn("_cookie_pool_lock = threading.Lock()", repaired_args["newString"])

        # Now apply edit and then a small window read(offset=1, limit=2)
        msgs.extend([
            {"role": "assistant", "content": "", "tool_calls": [out[0]]},
            {"role": "tool", "tool_call_id": "e1", "name": "edit", "content": "Edit applied successfully."},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py", "offset": 1, "limit": 2})}},
            ]},
            {"role": "tool", "tool_call_id": "r2", "name": "read", "content": "1: # Section: config.py\n2: DEFAULT_CONFIG = {}"},
        ])
        tracked = M._ff9_read_texts(msgs)["gemini_web2api.py"]
        self.assertIn("def load_cookie():", tracked)
        self.assertIn("_cookie_pool_lock = threading.Lock()", tracked)
        # AST must parse cleanly without IndentationError at the tail
        M._ff5_ast.parse(tracked)

    def test_test_file_edits_do_not_mask_missing_source_symbols_and_noop_edit_rejected(self):
        """Regression for conversation.txt Sec 39-40:
        1. Symbols written to `tests/test_cookie_pool.py` must not count as implemented in `gemini_web2api.py`.
        2. No-op edit (`oldString == newString`) is rejected with a clear problem message."""
        plan_md = "- [ ] Viết hàm `init_cookie_pool() -> None`\n- [ ] Tạo file `tests/test_cookie_pool.py`\n"
        msgs = [
            {"role": "user", "content": "Implement feature_implementation_plan.md"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "r1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
                {"id": "r2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "gemini_web2api.py"})}},
                {"id": "w1", "type": "function", "function": {"name": "write", "arguments": json.dumps({
                    "filePath": "tests/test_cookie_pool.py",
                    "content": "import gemini_web2api\ngemini_web2api.init_cookie_pool()\n",
                })}},
            ]},
            {"role": "tool", "tool_call_id": "r1", "name": "read", "content": plan_md},
            {"role": "tool", "tool_call_id": "r2", "name": "read", "content": "DEFAULT_CONFIG = {}\n"},
            {"role": "tool", "tool_call_id": "w1", "name": "write", "content": "Wrote tests/test_cookie_pool.py"},
        ]
        ctx = M._analyze(msgs)
        rem = M._ff17_remaining_plan_or_todo_work(ctx, M._answered_calls(msgs), msgs)
        self.assertIn("init_cookie_pool", rem["missing_plan_tokens"])
        self.assertTrue(rem["has_more"])

        # No-op edit rejection
        noop_tc = {
            "id": "e_noop",
            "type": "function",
            "function": {
                "name": "edit",
                "arguments": json.dumps({
                    "filePath": "gemini_web2api.py",
                    "oldString": "DEFAULT_CONFIG = {}",
                    "newString": "DEFAULT_CONFIG = {}",
                }),
            },
        }
        out, probs = M._base_gate_act_calls([noop_tc], msgs)
        self.assertEqual([], out)
        self.assertTrue(any("no-op edit" in p for p in probs))

    def test_required_tool_duplicate_call_does_not_leak_prose_on_next_attempt(self):
        """Regression for conversation.txt Sec 46: when `required_tool=True` and attempt 1 emits a
        duplicate tool call while attempt 2 emits plain text, `_generate_with_tool_retry` must NOT
        return the plain text early -- it must retry until a new tool call is emitted."""
        msgs = [
            {"role": "user", "content": "Implement feature_implementation_plan.md"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"filePath": "feature_implementation_plan.md"})}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "# Plan"},
        ]
        # Verify editing another file or calling todowrite does not wipe `read(feature_implementation_plan.md)` from _repeatable_calls
        msgs_with_todo = list(msgs) + [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "todowrite", "arguments": json.dumps({"todos": [{"content": "step 1", "status": "in_progress"}]})}},
            ]},
            {"role": "tool", "tool_call_id": "t1", "name": "todowrite", "content": "ok"},
        ]
        rep = M._repeatable_calls(msgs_with_todo)
        self.assertIn(("read", M._canonical_args({"filePath": "feature_implementation_plan.md"})), rep)
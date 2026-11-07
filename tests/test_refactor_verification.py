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
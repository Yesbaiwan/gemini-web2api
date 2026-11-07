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

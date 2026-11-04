import unittest
from unittest import mock

from gemini_web2api import multimodal as M
from gemini_web2api import tools as T
from gemini_web2api import flow_patch as F
from gemini_web2api.config import CONFIG

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _public(host):
    return "93.184.216.34" if host == "img.example.com" else None


@unittest.skipUnless(hasattr(M, "_ssrf7_hop"), "ssrf7 block not applied")
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


if __name__ == "__main__":
    unittest.main()

"""Guards for wiring that the stage-suffix refactor can silently drop.

Each check pins behaviour that the original (pre-refactor) file had and that no
other test covered: late rebinding of a name to the *last* stage of a chain.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gemini_web2api as G


class FinalBindingTests(unittest.TestCase):
    def test_log_is_flow_wrapper_and_records_to_ring_buffer(self):
        # flowfix13 wraps log() to keep a ring buffer used by the runtime check;
        # a later `import ... log_s1 as log` must not replace it with the raw log.
        self.assertIsNot(G.log, G.log_s1)
        G.log("refactor-regression-marker")   # ring buffer (maxlen 400) may already be full
        self.assertTrue(any("refactor-regression-marker" in l for l in G._RF_LOGS))

    def test_ff14_edit_anchor_uses_ff17_implementations(self):
        self.assertIs(G._ff14_find_unique, G._ff17_find_unique)
        self.assertIs(G._ff14_reindent, G._ff17_reindent)

    def test_fuzzy_edit_anchor_tolerates_blank_line_drift(self):
        hay = "def f():\n    a = 1\n\n    b = 2\n    return a + b\n"
        old = "    a = 1\n    b = 2"          # model dropped the blank line
        prev = G.CONFIG.get("flow17_fuzzy_edit")
        G.CONFIG["flow17_fuzzy_edit"] = True
        try:
            actual, n = G._ff14_find_unique(old, hay)
            self.assertEqual(1, n)
            self.assertEqual("    a = 1\n\n    b = 2", actual)
            G.CONFIG["flow17_fuzzy_edit"] = False
            actual_off, n_off = G._ff14_find_unique(old, hay)
            self.assertIsNone(actual_off)
        finally:
            if prev is None:
                G.CONFIG.pop("flow17_fuzzy_edit", None)
            else:
                G.CONFIG["flow17_fuzzy_edit"] = prev

    def test_test_command_regex_is_the_broad_final_one(self):
        # The flow section re-assigned _TEST_CMD_RE with a broader pattern; the
        # earlier, narrower definition must not be the one that survives.
        for cmd in ("yarn test", "npm run test", "cargo test", "go test ./...",
                    "python -m pytest -q", "python -m unittest discover"):
            self.assertTrue(G._TEST_CMD_RE.search(cmd), cmd)
        self.assertFalse(G._TEST_CMD_RE.search("git status"))

    def test_stage_helpers_referenced_by_final_stage_exist(self):
        for name in ("_ff14_find_unique_s1", "_ff14_reindent_s1"):
            self.assertTrue(callable(getattr(G, name)), name)


if __name__ == "__main__":
    unittest.main()

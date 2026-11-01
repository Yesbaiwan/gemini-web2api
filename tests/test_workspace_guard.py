import unittest

from gemini_web2api import tools
from gemini_web2api.config import CONFIG


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


if __name__ == "__main__":
    unittest.main()
